import json
import os
from dataclasses import replace
from unittest.mock import patch

import pytest

from migraflow.cli import main
from migraflow.config import Config, ConfigError, Endpoint, RetryPolicy
from migraflow.planner import create_plan
from migraflow.rsync_adapter import check_rsync, transfer_batch
from migraflow.state import database, summary
from migraflow.transfer import execute


@pytest.fixture
def config(tmp_path):
    source, target = tmp_path / "source", tmp_path / "destination"
    source.mkdir()
    target.mkdir()
    for name in ("unchanged", "changed", "removed"):
        (source / name).write_bytes(b"original")
    return Config(Endpoint("local", source), Endpoint("local", target), tmp_path / "state",
                  retry=RetryPolicy(3, 0, 0))


@pytest.fixture
def rsync():
    try:
        return check_rsync()
    except ConfigError as exc:
        pytest.skip(str(exc))


def completed(config):
    plan = create_plan(config)
    result = execute(config, plan["id"], allow_unprotected_source=True)
    assert result["status"] == "VERIFIED", result
    return result


@pytest.mark.parametrize("algorithm", ["sha256", "md5"])
def test_new_round_copies_only_changed_and_added_retains_removed(config, rsync, algorithm):
    config = replace(config, hash_algorithm=algorithm)
    baseline = completed(config)
    unchanged = config.destination.path / "unchanged"
    inode = unchanged.stat().st_ino
    (config.source.path / "changed").write_bytes(b"new contents")
    (config.source.path / "added").write_bytes(b"new")
    (config.source.path / "removed").unlink()
    planned = create_plan(config, baseline["id"])
    assert planned["baseline_id"] == baseline["id"]
    assert planned["deletion_candidates_complete"] is True
    assert planned["deletion_candidates"] == [{"path": "removed", "kind": "file"}]
    assert planned["counts"] == {"COPY_NEW": 1, "SOURCE_CHANGED": 1, "INCREMENTAL_CHECK": 1}
    copied = []

    def capture(*args, **kwargs):
        copied.extend(args[3])
        return transfer_batch(*args, **kwargs)

    with patch("migraflow.transfer.transfer_batch", side_effect=capture):
        result = execute(config, planned["id"], overwrite=True, allow_unprotected_source=True)
    assert result["status"] == "VERIFIED", result
    assert result["verified_files"] == 3
    assert result["reused_files"] == 1
    assert set(copied) == {b"added", b"changed"}
    assert unchanged.stat().st_ino == inode
    assert (config.destination.path / "removed").read_bytes() == b"original"
    with database(config.state_dir, readonly=True) as db:
        assert summary(db, baseline["id"])["status"] == "VERIFIED"
        assert db.execute("SELECT source_hash FROM file_results WHERE plan_id=? AND path=?", (baseline["id"], b"changed")).fetchone()[0] != db.execute(
            "SELECT source_hash FROM file_results WHERE plan_id=? AND path=?", (planned["id"], b"changed")).fetchone()[0]


def test_identical_round_needs_no_overwrite_or_copy(config, rsync):
    baseline = completed(config)
    planned = create_plan(config, baseline["id"])
    with patch("migraflow.transfer.transfer_batch") as copy:
        result = execute(config, planned["id"], allow_unprotected_source=True)
    copy.assert_not_called()
    assert result["status"] == "VERIFIED"
    assert result["reused_files"] == 3


@pytest.mark.parametrize("side", ["source", "destination"])
def test_same_size_mtime_content_change_requires_current_round_overwrite(config, rsync, side):
    baseline = completed(config)
    path = getattr(config, side).path / "changed"
    info = path.stat()
    path.write_bytes(b"Original")
    os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
    planned = create_plan(config, baseline["id"])
    assert planned["counts"]["INCREMENTAL_CHECK"] == 3
    before = (config.destination.path / "changed").read_bytes()
    with patch("migraflow.transfer.transfer_batch") as copy:
        result = execute(config, planned["id"], allow_unprotected_source=True)
    copy.assert_not_called()
    assert result["status"] == "FAILED"
    assert "--overwrite" in result["error"]
    assert (config.destination.path / "changed").read_bytes() == before
    result = execute(config, planned["id"], resume=True, overwrite=True, allow_unprotected_source=True)
    assert result["status"] == "VERIFIED", result
    assert (config.destination.path / "changed").read_bytes() == (config.source.path / "changed").read_bytes()


def test_removed_directory_and_renamed_file_are_retained(config, rsync):
    (config.source.path / "old-dir").mkdir()
    (config.source.path / "old-dir" / "file").write_bytes(b"keep")
    baseline = completed(config)
    (config.source.path / "old-dir" / "file").unlink()
    (config.source.path / "old-dir").rmdir()
    (config.source.path / "changed").rename(config.source.path / "renamed")
    planned = create_plan(config, baseline["id"])
    assert planned["deletion_candidates_count"] == 3
    result = execute(config, planned["id"], allow_unprotected_source=True)
    assert result["status"] == "VERIFIED", result
    assert (config.destination.path / "old-dir" / "file").read_bytes() == b"keep"
    assert (config.destination.path / "changed").exists()
    assert (config.destination.path / "renamed").read_bytes() == b"original"


def test_missing_target_is_copied_without_overwrite(config, rsync):
    baseline = completed(config)
    (config.destination.path / "changed").unlink()
    planned = create_plan(config, baseline["id"])
    assert planned["counts"]["RESTORE_MISSING"] == 1
    with patch("migraflow.transfer.transfer_batch", wraps=transfer_batch) as copy:
        result = execute(config, planned["id"], allow_unprotected_source=True)
    assert result["status"] == "VERIFIED"
    assert copy.call_args.args[3] == [b"changed"]


def test_unrelated_existing_target_still_requires_overwrite(config, rsync):
    baseline = completed(config)
    (config.source.path / "new").write_bytes(b"same")
    (config.destination.path / "new").write_bytes(b"same")
    planned = create_plan(config, baseline["id"])
    with pytest.raises(ConfigError, match="--overwrite"):
        execute(config, planned["id"], allow_unprotected_source=True)


@pytest.mark.parametrize("change", ["algorithm", "target", "status", "fingerprint", "identity"])
def test_incompatible_or_unfinished_baseline_rejected(config, rsync, change):
    baseline = completed(config)
    if change == "algorithm":
        config = replace(config, hash_algorithm="md5")
    elif change == "target":
        different = config.destination.path.parent / "different"
        different.mkdir()
        config = replace(config, destination=replace(config.destination, path=different))
    elif change in ("status", "fingerprint"):
        with database(config.state_dir) as db:
            db.execute("UPDATE plans SET " + ("status='FAILED'" if change == "status" else "fingerprint='bad'") + " WHERE id=?", (baseline["id"],))
            db.commit()
    else:
        old = config.source.path.with_name("old-source")
        config.source.path.rename(old)
        config.source.path.mkdir()
    with pytest.raises(ConfigError, match="baseline|Baseline"):
        create_plan(config, baseline["id"])


def test_incomplete_scan_does_not_produce_deletion_candidates(config, rsync):
    baseline = completed(config)
    with patch("migraflow.planner._scan", side_effect=OSError("scan interrupted")):
        planned = create_plan(config, baseline["id"])
    assert planned["status"] == "FAILED"
    assert planned["deletion_candidates_complete"] is False
    assert "deletion_candidates" not in planned


def test_incremental_interruption_after_publish_resumes_without_recopy(config, rsync):
    baseline = completed(config)
    (config.source.path / "changed").write_bytes(b"updated")
    planned = create_plan(config, baseline["id"])

    def interrupt(*args, **kwargs):
        transfer_batch(*args, **kwargs)
        raise KeyboardInterrupt

    with patch("migraflow.transfer.transfer_batch", side_effect=interrupt), pytest.raises(KeyboardInterrupt):
        execute(config, planned["id"], overwrite=True, allow_unprotected_source=True)
    with patch("migraflow.transfer.transfer_batch") as copy:
        result = execute(config, planned["id"], resume=True, allow_unprotected_source=True)
    copy.assert_not_called()
    assert result["status"] == "VERIFIED"
    assert result["reused_files"] == 3


def test_seeding_interruption_recovers_missing_checkpoints(config, rsync):
    baseline = completed(config)
    planned = create_plan(config, baseline["id"])
    from migraflow.transfer import _seed_incremental

    def interrupted_seed(connection, plan_id, settings, checked):
        _seed_incremental(connection, plan_id, settings, checked)
        connection.execute("DELETE FROM file_results WHERE plan_id=? AND path=?", (plan_id, b"unchanged"))
        connection.commit()
        raise KeyboardInterrupt

    with patch("migraflow.transfer._seed_incremental", side_effect=interrupted_seed), pytest.raises(KeyboardInterrupt):
        execute(config, planned["id"], allow_unprotected_source=True)
    with patch("migraflow.transfer.transfer_batch") as copy:
        result = execute(config, planned["id"], resume=True, allow_unprotected_source=True)
    copy.assert_not_called()
    assert result["status"] == "VERIFIED"


def test_cli_incremental_plan_json(config, rsync, tmp_path, monkeypatch, capsys):
    baseline = completed(config)
    monkeypatch.chdir(tmp_path)
    arguments = ["--source", str(config.source.path), "--destination", str(config.destination.path),
                 "--state-dir", str(config.state_dir), "--json"]
    assert main(["plan", "--baseline", baseline["id"], *arguments]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["baseline_id"] == baseline["id"]
    assert main(["run", "--job", plan["id"], "--allow-unprotected-source", *arguments]) == 0
    assert json.loads(capsys.readouterr().out)["reused_files"] == 3


def test_v3_database_upgrades_preserving_baseline(config, rsync):
    baseline = completed(config)
    with database(config.state_dir) as db:
        db.execute("DROP TABLE plan_lineage")
        db.execute("PRAGMA user_version=3")
        db.commit()
    planned = create_plan(config, baseline["id"])
    assert execute(config, planned["id"], allow_unprotected_source=True)["status"] == "VERIFIED"
    with database(config.state_dir, readonly=True) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 6


def test_multibatch_identical_round_preserves_source_and_target(config, rsync):
    for index in range(1001):
        (config.source.path / f"batch-{index:04d}").write_bytes(b"data")
    baseline = completed(config)
    before = {path.name: (path.stat().st_ino, path.stat().st_mtime_ns)
              for path in config.destination.path.iterdir()}
    planned = create_plan(config, baseline["id"])
    assert before == {path.name: (path.stat().st_ino, path.stat().st_mtime_ns)
                      for path in config.destination.path.iterdir()}
    with patch("migraflow.transfer.transfer_batch") as copy:
        result = execute(config, planned["id"], allow_unprotected_source=True)
    copy.assert_not_called()
    assert result["reused_files"] == 1004
    assert before == {path.name: (path.stat().st_ino, path.stat().st_mtime_ns)
                      for path in config.destination.path.iterdir()}


def test_previous_round_overwrite_does_not_authorize_next_round(config, rsync):
    first = create_plan(config)
    assert execute(config, first["id"], overwrite=True, allow_unprotected_source=True)["status"] == "VERIFIED"
    (config.source.path / "changed").write_bytes(b"updated")
    planned = create_plan(config, first["id"])
    with patch("migraflow.transfer.transfer_batch") as copy:
        result = execute(config, planned["id"], allow_unprotected_source=True)
    copy.assert_not_called()
    assert result["status"] == "FAILED"
    assert "--overwrite" in result["error"]


@pytest.mark.parametrize("nested", [False, True])
def test_scan_generation_change_keeps_deletion_candidates_unconfirmed(config, rsync, nested):
    folder = config.source.path / "nested" if nested else config.source.path
    folder.mkdir(exist_ok=True)
    (folder / "removed").write_bytes(b"data")
    baseline = completed(config)
    from migraflow.planner import _scan

    def changed_after_scan(*args, **kwargs):
        _scan(*args, **kwargs)
        (folder / "removed").unlink()

    with patch("migraflow.planner._scan", side_effect=changed_after_scan):
        planned = create_plan(config, baseline["id"])
    assert planned["status"] == "FAILED"
    assert planned["deletion_candidates_complete"] is False
    assert "deletion_candidates" not in planned
