import errno
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from migraflow.config import Config, ConfigError, Endpoint, RetryPolicy, load_config
from migraflow.planner import create_plan
from migraflow.rsync_adapter import RsyncError, check_rsync, transfer_batch
from migraflow.locking import file_lock
from migraflow.state import database
from migraflow.transfer import execute


@pytest.mark.parametrize("change", ["size", "mtime", "directory", "missing"])
def test_resume_invalidates_verified_evidence_on_precheck_change(config, change):
    check_rsync()
    folder = config.source.path / "nested"
    folder.mkdir()
    source = folder / "file"
    source.write_bytes(b"original")
    job = create_plan(config)["id"]
    assert execute(config, job, allow_unprotected_source=True)["status"] == "VERIFIED"
    if change == "size":
        source.write_bytes(b"different size")
    elif change == "mtime":
        info = source.stat()
        os.utime(source, ns=(info.st_atime_ns, info.st_mtime_ns + 2_000_000_000))
    elif change == "missing":
        info = folder.stat()
        source.unlink()
        os.utime(folder, ns=(info.st_atime_ns, info.st_mtime_ns))
    else:
        info = folder.stat()
        os.utime(folder, ns=(info.st_atime_ns, info.st_mtime_ns + 2_000_000_000))
    result = execute(config, job, resume=True, allow_unprotected_source=True)
    assert result["status"] == "VERIFY_FAILED"
    assert result["verified_files"] == 0
    assert (config.destination.path / "nested" / "file").read_bytes() == b"original"
    with database(config.state_dir, readonly=True) as db:
        row = db.execute("SELECT source_hash,destination_hash FROM file_results WHERE plan_id=?", (job,)).fetchone()
        assert tuple(row) == (None, None)


@pytest.fixture
def config(tmp_path):
    source, target = tmp_path / "source", tmp_path / "destination"
    source.mkdir()
    target.mkdir()
    return Config(Endpoint("local", source), Endpoint("local", target), tmp_path / "state",
                  retry=RetryPolicy(3, 0, 0))


@pytest.fixture
def rsync():
    try:
        return check_rsync()
    except ConfigError as exc:
        pytest.skip(str(exc))


@pytest.mark.parametrize("algorithm", ["sha256", "md5"])
def test_resume_verified_files_without_copying(config, rsync, algorithm):
    config = replace(config, hash_algorithm=algorithm)
    (config.source.path / "file").write_bytes(b"data")
    planned = create_plan(config)
    assert execute(config, planned["id"], allow_unprotected_source=True)["status"] == "VERIFIED"
    target = config.destination.path / "file"
    original_inode = target.stat().st_ino
    with patch("migraflow.transfer.transfer_batch") as copy:
        result = execute(config, planned["id"], resume=True, allow_unprotected_source=True)
    copy.assert_not_called()
    assert result["status"] == "VERIFIED"
    assert result["reused_files"] == 1
    assert target.stat().st_ino == original_inode


def test_resume_repairs_tampered_owned_file(config, rsync):
    (config.source.path / "file").write_bytes(b"right")
    planned = create_plan(config)
    assert execute(config, planned["id"], allow_unprotected_source=True)["status"] == "VERIFIED"
    target = config.destination.path / "file"
    previous = target.stat()
    target.write_bytes(b"wrong")
    os.utime(target, ns=(previous.st_atime_ns, previous.st_mtime_ns))
    with patch("migraflow.transfer.transfer_batch", wraps=transfer_batch) as copy:
        result = execute(config, planned["id"], resume=True, allow_unprotected_source=True)
    assert copy.call_count == 1
    assert result["status"] == "VERIFIED"
    assert result["reused_files"] == 0
    assert target.read_bytes() == b"right"


def test_resume_after_publish_before_database_commit_does_not_recopy(config, rsync):
    (config.source.path / "file").write_bytes(b"data")
    planned = create_plan(config)

    def crash_after_publish(*args, **kwargs):
        transfer_batch(*args, **kwargs)
        raise KeyboardInterrupt

    with patch("migraflow.transfer.transfer_batch", side_effect=crash_after_publish):
        with pytest.raises(KeyboardInterrupt):
            execute(config, planned["id"], allow_unprotected_source=True)
    with patch("migraflow.transfer.transfer_batch") as copy:
        result = execute(config, planned["id"], resume=True, allow_unprotected_source=True)
    copy.assert_not_called()
    assert result["status"] == "VERIFIED"
    assert result["reused_files"] == 1


@pytest.mark.parametrize("failure", [RsyncError(10, "socket reset"), OSError(errno.EAGAIN, "temporary")])
def test_transient_error_retries_then_succeeds(config, rsync, failure):
    (config.source.path / "file").write_text("data")
    planned = create_plan(config)
    calls = 0

    def flaky(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise failure
        return transfer_batch(*args, **kwargs)

    with patch("migraflow.transfer.transfer_batch", side_effect=flaky):
        result = execute(config, planned["id"], allow_unprotected_source=True)
    assert result["status"] == "VERIFIED"
    assert calls == 2
    assert result["retry_count"] == 1


@pytest.mark.parametrize("failure", [RsyncError(23, "partial error"), PermissionError(errno.EACCES, "denied"),
                                    OSError(errno.ENOSPC, "full"), RsyncError(24, "source vanished")])
def test_permanent_error_not_retried(config, failure):
    (config.source.path / "file").write_text("data")
    planned = create_plan(config)
    with patch("migraflow.transfer.check_rsync", return_value=["rsync"]), patch(
            "migraflow.transfer.transfer_batch", side_effect=failure) as copy:
        result = execute(config, planned["id"], allow_unprotected_source=True)
    assert copy.call_count == 1
    assert result["status"] == "FAILED"
    assert result["retry_count"] == 0


def test_retry_budget_and_exponential_backoff(config):
    config = replace(config, retry=RetryPolicy(3, 2, 3))
    (config.source.path / "file").write_text("data")
    planned = create_plan(config)
    with patch("migraflow.transfer.check_rsync", return_value=["rsync"]), patch(
            "migraflow.transfer.transfer_batch", side_effect=RsyncError(30, "timeout")) as copy, patch(
            "migraflow.transfer.time.sleep") as sleep:
        result = execute(config, planned["id"], allow_unprotected_source=True)
    assert copy.call_count == 3
    assert [call.args[0] for call in sleep.call_args_list] == [2, 3]
    assert result["status"] == "FAILED"
    assert result["retry_count"] == 2


def test_retry_settings_can_change_without_invalidating_plan(config):
    planned = create_plan(config)
    different = replace(config, retry=RetryPolicy(1, 0, 0))
    with patch("migraflow.transfer.check_rsync", return_value=["rsync"]):
        result = execute(different, planned["id"], allow_unprotected_source=True)
    assert result["status"] == "VERIFIED"  # empty inventory needs no rsync calls


@pytest.mark.parametrize("retry", [None, [], {"max_attempts": 0}, {"max_attempts": True},
                                  {"max_attempts": 11}, {"initial_delay_seconds": -1},
                                  {"initial_delay_seconds": float("nan")},
                                  {"max_delay_seconds": float("inf")},
                                  {"initial_delay_seconds": 5, "max_delay_seconds": 1}, {"typo": 3}])
def test_invalid_retry_configuration_rejected(config, tmp_path, retry):
    path = tmp_path / "migration.yml"
    path.write_text(yaml.safe_dump({"source": {"path": str(config.source.path)},
                                   "destination": {"path": str(config.destination.path)}, "retry": retry}), encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path)


def test_temporary_hash_read_error_retried(config, rsync):
    (config.source.path / "file").write_bytes(b"data")
    planned = create_plan(config)
    from migraflow.verifier import hash_file
    count = 0

    def flaky(*args):
        nonlocal count
        count += 1
        if count == 1:
            raise OSError(errno.EAGAIN, "read temporary")
        return hash_file(*args)

    with patch("migraflow.transfer.hash_file", side_effect=flaky):
        result = execute(config, planned["id"], allow_unprotected_source=True)
    assert result["status"] == "VERIFIED"
    assert result["retry_count"] == 1


def test_process_kill_mid_transfer_resumes_without_recopying_checkpoint(config, rsync, tmp_path):
    for index in range(500):
        (config.source.path / f"a-{index:04d}").write_bytes(b"done")
    large = config.source.path / "z-large"
    with large.open("wb") as handle:
        for _ in range(32):
            handle.write(b"x" * (1024 * 1024))
    planned = create_plan(config)
    settings = {"source": {"path": str(config.source.path)}, "destination": {"path": str(config.destination.path)},
                "state_dir": str(config.state_dir), "retry": {"initial_delay_seconds": 0, "max_delay_seconds": 0}}
    file = tmp_path / "migration.yml"
    file.write_text(yaml.safe_dump(settings), encoding="utf-8")
    # Real rsync, deliberately throttled only in the test subprocess.
    program = """import sys
from migraflow.cli import main
import migraflow.transfer as transfer
from migraflow.rsync_adapter import command_prefix
transfer.check_rsync = lambda: command_prefix() + ['--bwlimit=512']
sys.exit(main(sys.argv[1:]))
"""
    with (tmp_path / "coordinator.log").open("wb") as output:
        process = subprocess.Popen([sys.executable, "-c", program, "run", "--config", str(file),
                                    "--job", planned["id"], "--allow-unprotected-source", "--json"],
                                   stdout=output, stderr=output)
        try:
            deadline = time.monotonic() + 50
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    pytest.fail("Coordinator exited before kill: " + (tmp_path / "coordinator.log").read_text(errors="replace"))
                with database(config.state_dir, readonly=True) as db:
                    verified = db.execute("SELECT COUNT(*) FROM file_results WHERE status='VERIFIED'").fetchone()[0]
                active = list(config.destination.path.glob(".z-large.*"))
                active += list((config.destination.path / (".migraflow-partial-" + planned["id"])).glob("*"))
                if verified == 500 and any(p.is_file() and p.stat().st_size >= 65536 for p in active):
                    break
                time.sleep(0.1)
            else:
                pytest.fail("First batch did not reach a durable verification checkpoint.")
            before = {p.name: p.stat().st_ino for p in config.destination.path.glob("a-*")}
            process.kill()  # abrupt death, not a caught Python exception
            process.wait(timeout=10)
            calls = []
            partial_sizes = []

            def capture(*args, **kwargs):
                calls.extend(args[3])
                partial = config.destination.path / (".migraflow-partial-" + planned["id"]) / "z-large"
                partial_sizes.append(partial.stat().st_size if partial.exists() else 0)
                return transfer_batch(*args, **kwargs)

            with patch("migraflow.transfer.transfer_batch", side_effect=capture):
                result = execute(config, planned["id"], resume=True, allow_unprotected_source=True)
            assert result["status"] == "VERIFIED", result
            assert result["reused_files"] >= 500
            assert any(size > 0 for size in partial_sizes)
            assert not any(os.fsdecode(raw).startswith("a-") for raw in calls)
            assert {p.name: p.stat().st_ino for p in config.destination.path.glob("a-*")} == before
            assert (config.destination.path / "z-large").read_bytes() == large.read_bytes()
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)


def test_resume_rejects_changed_source_without_copying(config, rsync):
    source = config.source.path / "file"
    source.write_bytes(b"right")
    planned = create_plan(config)
    assert execute(config, planned["id"], allow_unprotected_source=True)["status"] == "VERIFIED"
    previous = source.stat()
    source.write_bytes(b"wrong")
    os.utime(source, ns=(previous.st_atime_ns, previous.st_mtime_ns))
    with patch("migraflow.transfer.transfer_batch") as copy:
        result = execute(config, planned["id"], resume=True, allow_unprotected_source=True)
    copy.assert_not_called()
    assert result["status"] == "VERIFY_FAILED"
    assert result["verified_files"] == 0
    assert (config.destination.path / "file").read_bytes() == b"right"


def test_worker_lease_blocks_resume(config):
    planned = create_plan(config)
    with file_lock(config.state_dir / "worker.lock"), patch("migraflow.transfer.time.monotonic", side_effect=[0, 11]):
        with pytest.raises(ConfigError, match="still active"):
            execute(config, planned["id"], resume=True, allow_unprotected_source=True)


def test_resume_rejects_algorithm_change(config, rsync):
    (config.source.path / "file").write_bytes(b"data")
    planned = create_plan(config)
    assert execute(config, planned["id"], allow_unprotected_source=True)["status"] == "VERIFIED"
    with pytest.raises(ConfigError, match="Configuration differs"):
        execute(replace(config, hash_algorithm="md5"), planned["id"], resume=True, allow_unprotected_source=True)


def test_version_two_checkpoint_migrates_and_reuses_hashes(config, rsync):
    (config.source.path / "file").write_bytes(b"data")
    planned = create_plan(config)
    assert execute(config, planned["id"], allow_unprotected_source=True)["status"] == "VERIFIED"
    with database(config.state_dir) as db:
        db.execute("ALTER TABLE file_results DROP COLUMN copied")
        db.execute("DROP TABLE attempts")
        db.execute("PRAGMA user_version=2")
        saved = json.loads(db.execute("SELECT config FROM plans WHERE id=?", (planned["id"],)).fetchone()[0])
        saved.pop("retry")
        serialized = json.dumps(saved, sort_keys=True, ensure_ascii=True)
        db.execute("UPDATE plans SET config=?,fingerprint=? WHERE id=?",
                   (serialized, hashlib.sha256(serialized.encode()).hexdigest(), planned["id"]))
        db.commit()
    with patch("migraflow.transfer.transfer_batch") as copy:
        result = execute(config, planned["id"], resume=True, allow_unprotected_source=True)
    copy.assert_not_called()
    assert result["status"] == "VERIFIED"
    assert result["reused_files"] == 1


def test_interruption_during_verification_resumes_without_copy(config, rsync):
    (config.source.path / "file").write_bytes(b"data")
    planned = create_plan(config)
    assert execute(config, planned["id"], allow_unprotected_source=True)["status"] == "VERIFIED"
    with patch("migraflow.transfer.hash_file", side_effect=KeyboardInterrupt):
        with pytest.raises(KeyboardInterrupt):
            execute(config, planned["id"], verify_only=True)
    with patch("migraflow.transfer.transfer_batch") as copy:
        result = execute(config, planned["id"], resume=True, allow_unprotected_source=True)
    copy.assert_not_called()
    assert result["status"] == "VERIFIED"


def test_second_batch_failure_retains_first_verified_checkpoint(config, rsync):
    for index in range(501):
        (config.source.path / f"file-{index:04d}").write_bytes(b"data")
    planned = create_plan(config)
    count = 0

    def partial(*args, **kwargs):
        nonlocal count
        count += 1
        if count == 2:
            raise OSError(errno.ENOSPC, "full")
        return transfer_batch(*args, **kwargs)

    with patch("migraflow.transfer.transfer_batch", side_effect=partial):
        first = execute(config, planned["id"], allow_unprotected_source=True)
    assert first["status"] == "FAILED"
    assert first["verified_files"] == 500
    with patch("migraflow.transfer.transfer_batch", wraps=transfer_batch) as copy:
        result = execute(config, planned["id"], resume=True, allow_unprotected_source=True)
    assert copy.call_count == 1
    assert copy.call_args.args[3] == [b"file-0500"]
    assert result["status"] == "VERIFIED"
