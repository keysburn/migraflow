import hashlib
import json
import os
import subprocess
from unittest.mock import patch

import pytest

from migraflow.cli import main
from migraflow.config import Config, ConfigError, Endpoint
from migraflow.planner import create_plan
from migraflow.rsync_adapter import check_rsync, rsync_path
from migraflow.state import database
from migraflow.transfer import execute
from migraflow.verifier import VerificationError, hash_file


@pytest.fixture
def config(tmp_path):
    source, destination = tmp_path / "source", tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    return Config(Endpoint("local", source), Endpoint("local", destination), tmp_path / "state")


@pytest.fixture
def rsync():
    try:
        return check_rsync()
    except ConfigError as exc:
        pytest.skip(str(exc))


@pytest.mark.parametrize("algorithm", ["sha256", "md5"])
def test_real_rsync_copy_and_full_verification(config, rsync, algorithm):
    config = Config(config.source, config.destination, config.state_dir, algorithm)
    source = config.source.path
    (source / "nested").mkdir()
    (source / "nested" / "empty-dir").mkdir()
    (source / "nested" / "한글 space.txt").write_text("data", encoding="utf-8")
    (source / "empty").touch()
    (source / "-option-like.txt").write_bytes(b"x" * (2 * 1024 * 1024 + 3))
    before = {str(p.relative_to(source)): (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_mode)
              for p in source.rglob("*") if p.is_file()}
    planned = create_plan(config)
    result = execute(config, planned["id"], allow_unprotected_source=True)
    assert result["status"] == "VERIFIED", result
    assert result["verified_files"] == 3
    assert result["cutover_ready"] is False
    for name, (data, mtime, mode) in before.items():
        src, dst = source / name, config.destination.path / name
        assert src.read_bytes() == dst.read_bytes() == data
        assert src.stat().st_mtime_ns == dst.stat().st_mtime_ns == mtime
        assert src.stat().st_mode == mode
    assert (config.destination.path / "nested" / "empty-dir").is_dir()
    with database(config.state_dir, readonly=True) as db:
        for row in db.execute("SELECT * FROM file_results WHERE plan_id=?", (planned["id"],)):
            expected = hashlib.new(algorithm, (source / os.fsdecode(row["path"])).read_bytes()).hexdigest()
            assert row["algorithm"] == algorithm
            assert row["source_hash"] == row["destination_hash"] == expected


def test_overwrite_requires_explicit_option_and_keeps_destination_only_files(config, rsync):
    (config.source.path / "replace").write_text("new contents")
    (config.destination.path / "replace").write_text("old")
    (config.destination.path / "extra").write_text("keep")
    planned = create_plan(config)
    with pytest.raises(ConfigError, match="--overwrite"):
        execute(config, planned["id"], allow_unprotected_source=True)
    assert (config.destination.path / "replace").read_text() == "old"
    result = execute(config, planned["id"], allow_unprotected_source=True, overwrite=True)
    assert result["status"] == "VERIFIED", result
    assert (config.destination.path / "replace").read_text() == "new contents"
    assert (config.destination.path / "extra").read_text() == "keep"


@pytest.mark.parametrize("algorithm", ["sha256", "md5"])
def test_one_byte_tamper_detected_even_with_same_size_and_mtime(config, rsync, algorithm):
    config = Config(config.source, config.destination, config.state_dir, algorithm)
    (config.source.path / "file").write_bytes(b"original")
    planned = create_plan(config)
    assert execute(config, planned["id"], allow_unprotected_source=True)["status"] == "VERIFIED"
    target = config.destination.path / "file"
    info = target.stat()
    target.write_bytes(b"Original")
    os.utime(target, ns=(info.st_atime_ns, info.st_mtime_ns))
    result = execute(config, planned["id"], verify_only=True)
    assert result["status"] == "VERIFY_FAILED"
    assert result["verified_files"] == 0
    assert "hashes differ" in result["issues"][0]["detail"]


def test_quick_match_file_is_actually_replaced_and_verified(config, rsync):
    source, target = config.source.path / "file", config.destination.path / "file"
    source.write_bytes(b"right")
    target.write_bytes(b"wrong")
    info = source.stat()
    os.utime(target, ns=(info.st_atime_ns, info.st_mtime_ns))
    planned = create_plan(config)
    assert planned["counts"]["QUICK_MATCH_UNVERIFIED"] == 1
    result = execute(config, planned["id"], overwrite=True, allow_unprotected_source=True)
    assert result["status"] == "VERIFIED", result
    assert target.read_bytes() == b"right"


def test_unprotected_source_blocked_by_default(config):
    planned = create_plan(config)
    with pytest.raises(ConfigError, match="read-only protection"):
        execute(config, planned["id"])
    assert not list(config.destination.path.iterdir())


def test_algorithm_change_cannot_reuse_plan(config):
    planned = create_plan(config)
    different = Config(config.source, config.destination, config.state_dir, "md5")
    with pytest.raises(ConfigError, match="Configuration differs"):
        execute(different, planned["id"], allow_unprotected_source=True)


def test_source_root_addition_invalidates_plan(config):
    planned = create_plan(config)
    (config.source.path / "added").write_text("new")
    with pytest.raises(VerificationError, match="root changed"):
        execute(config, planned["id"], allow_unprotected_source=True)
    assert not list(config.destination.path.iterdir())


def test_source_change_while_hashing_is_rejected(tmp_path):
    file = tmp_path / "file"
    file.write_bytes(b"old")
    factory = hashlib.new

    def tampering_digest(algorithm):
        digest = factory(algorithm)

        class Digest:
            def update(self, chunk):
                file.write_bytes(b"modified")
                digest.update(chunk)

            def hexdigest(self):
                return digest.hexdigest()

        return Digest()

    with patch("migraflow.verifier.hashlib.new", side_effect=tampering_digest):
        with pytest.raises(VerificationError, match="changed while hashing"):
            hash_file(file, "sha256")


def test_rsync_nonzero_never_marks_transfer_complete(config):
    (config.source.path / "file").write_text("data")
    planned = create_plan(config)
    with patch("migraflow.transfer.check_rsync", return_value=["rsync"]), patch(
            "migraflow.transfer.transfer_batch", side_effect=OSError("rsync returned 23")):
        result = execute(config, planned["id"], allow_unprotected_source=True)
    assert result["status"] == "FAILED"
    assert result["verified_files"] == 0
    assert result["file_statuses"] == {"TRANSFERRING": 1}


def test_missing_rsync_does_not_modify_destination(config):
    (config.source.path / "file").write_text("data")
    planned = create_plan(config)
    with patch("migraflow.transfer.check_rsync", side_effect=ConfigError("Install rsync")):
        with pytest.raises(ConfigError, match="Install rsync"):
            execute(config, planned["id"], allow_unprotected_source=True)
    assert not list(config.destination.path.iterdir())


def test_cli_run_verify_and_hash_failure_exit_code(config, rsync, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (config.source.path / "file").write_text("content")
    arguments = ["--source", str(config.source.path), "--destination", str(config.destination.path),
                 "--state-dir", str(config.state_dir), "--json"]
    assert main(["plan", *arguments]) == 0
    planned = json.loads(capsys.readouterr().out)
    assert main(["run", "--job", planned["id"], "--allow-unprotected-source", *arguments]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "VERIFIED"
    (config.destination.path / "file").write_text("changed")
    assert main(["verify", "--job", planned["id"], *arguments]) == 3
    assert json.loads(capsys.readouterr().out)["status"] == "VERIFY_FAILED"


def test_run_interruption_recorded(config):
    (config.source.path / "file").write_text("data")
    planned = create_plan(config)
    with patch("migraflow.transfer.check_rsync", return_value=["rsync"]), patch(
            "migraflow.transfer.transfer_batch", side_effect=KeyboardInterrupt):
        with pytest.raises(KeyboardInterrupt):
            execute(config, planned["id"], allow_unprotected_source=True)
    with database(config.state_dir, readonly=True) as db:
        assert db.execute("SELECT status FROM plans WHERE id=?", (planned["id"],)).fetchone()[0] == "INTERRUPTED"


def test_empty_source_is_verified_without_destination_deletion(config, rsync):
    (config.destination.path / "extra").write_text("keep")
    planned = create_plan(config)
    result = execute(config, planned["id"], allow_unprotected_source=True)
    assert result["status"] == "VERIFIED"
    assert result["verified_files"] == 0
    assert (config.destination.path / "extra").read_text() == "keep"


def test_target_appearing_after_plan_requires_overwrite(config, rsync):
    (config.source.path / "file").write_text("source")
    planned = create_plan(config)
    (config.destination.path / "file").write_text("appeared")
    with pytest.raises(ConfigError, match="--overwrite"):
        execute(config, planned["id"], allow_unprotected_source=True)
    assert (config.destination.path / "file").read_text() == "appeared"


def test_reverification_clears_previous_directory_failure(config, rsync):
    (config.source.path / "directory").mkdir()
    planned = create_plan(config)
    assert execute(config, planned["id"], allow_unprotected_source=True)["status"] == "VERIFIED"
    target = config.destination.path / "directory"
    source_info = (config.source.path / "directory").stat()
    os.utime(target, ns=(source_info.st_atime_ns, source_info.st_mtime_ns + 1000000000))
    assert execute(config, planned["id"], verify_only=True)["status"] == "VERIFY_FAILED"
    os.utime(target, ns=(source_info.st_atime_ns, source_info.st_mtime_ns))
    result = execute(config, planned["id"], verify_only=True)
    assert result["status"] == "VERIFIED"
    assert result["issues"] == []
    assert result["verified_files"] == 0


def test_source_generation_change_after_transfer_invalidates_verification(config, rsync):
    file = config.source.path / "file"
    file.write_text("original")
    planned = create_plan(config)
    assert execute(config, planned["id"], allow_unprotected_source=True)["status"] == "VERIFIED"
    file.write_text("modified")
    result = execute(config, planned["id"], verify_only=True)
    assert result["status"] == "VERIFY_FAILED"
    assert result["verified_files"] == 0


def test_multi_batch_real_transfer(config, rsync):
    for index in range(501):
        (config.source.path / f"file-{index:04d}").write_bytes(bytes([index % 256]))
    planned = create_plan(config)
    result = execute(config, planned["id"], allow_unprotected_source=True)
    assert result["status"] == "VERIFIED", result
    assert result["verified_files"] == 501


@pytest.mark.skipif(os.name == "nt", reason="POSIX-only filename cases")
def test_posix_newline_and_non_utf8_names(config, rsync):
    (config.source.path / "line\nbreak").write_bytes(b"newline")
    filename = os.fsencode(config.source.path) + b"/invalid-\xff-name"
    with open(filename, "wb") as handle:
        handle.write(b"non-UTF8")
    planned = create_plan(config)
    result = execute(config, planned["id"], allow_unprotected_source=True)
    assert result["status"] == "VERIFIED", result
    assert result["verified_files"] == 2


def test_version_one_database_upgrades_preserving_plan(config):
    planned = create_plan(config)
    with database(config.state_dir) as db:
        db.execute("DROP TABLE file_results")
        db.execute("PRAGMA user_version=1")
        db.commit()
    with database(config.state_dir, readonly=True) as db:
        assert db.execute("SELECT id FROM plans").fetchone()[0] == planned["id"]
    with database(config.state_dir) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 3
        assert db.execute("SELECT COUNT(*) FROM file_results").fetchone()[0] == 0
        assert db.execute("SELECT id FROM plans").fetchone()[0] == planned["id"]


def test_source_root_change_clears_previous_verification(config, rsync):
    (config.source.path / "file").write_text("data")
    planned = create_plan(config)
    assert execute(config, planned["id"], allow_unprotected_source=True)["status"] == "VERIFIED"
    (config.source.path / "added").write_text("new")
    result = execute(config, planned["id"], verify_only=True)
    assert result["status"] == "VERIFY_FAILED"
    assert result["verified_files"] == 0
    with database(config.state_dir, readonly=True) as db:
        row = db.execute("SELECT source_hash,destination_hash FROM file_results WHERE plan_id=?", (planned["id"],)).fetchone()
        assert tuple(row) == (None, None)


def test_wsl_path_timeout_is_a_controlled_error(config):
    with patch("migraflow.rsync_adapter.os.name", "nt"), patch(
            "migraflow.rsync_adapter.subprocess.run", side_effect=subprocess.TimeoutExpired("wslpath", 30)):
        with pytest.raises(ConfigError, match="path translation failed"):
            rsync_path(config.source.path)
