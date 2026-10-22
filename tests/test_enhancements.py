import json
import os
import struct
from dataclasses import replace
from io import StringIO
from pathlib import Path
from unittest.mock import patch
from contextlib import contextmanager

import pytest
from rich.console import Console

from migraflow.cli import main
from migraflow.config import Config, ConfigError, Endpoint, load_config
from migraflow.metadata import capture
from migraflow.monitor import watch
from migraflow.planner import create_plan
from migraflow.progress import Dashboard
from migraflow.reporting import cutover_report
from migraflow.rsync_adapter import check_rsync
from migraflow.state import database, summary
from migraflow.transfer import execute
from migraflow.verifier import VerificationError


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


def test_report_pending_queue_uses_index_and_retains_all_extras(config, rsync):
    import migraflow.reporting as reporting
    planned = create_plan(config)
    assert execute(config, planned["id"], allow_unprotected_source=True)["status"] == "VERIFIED"
    for index in range(80):
        (config.destination.path / f"extra-{index}").mkdir()
    connections, plans = [], []
    original_scan = reporting.scandir

    @contextmanager
    def connected(*args, **kwargs):
        with database(*args, **kwargs) as db:
            connections.append(db)
            yield db

    def scan(path):
        query = connections[-1].execute("EXPLAIN QUERY PLAN SELECT path FROM report_queue WHERE done=0 LIMIT 1").fetchall()
        plans.extend(row[3] for row in query)
        return original_scan(path)

    with patch("migraflow.transfer.database", connected), patch.object(reporting, "scandir", scan):
        report = cutover_report(config, planned["id"])
    assert report["checks"]["target_inventory_complete"]
    assert report["target_extras_count"] == 80
    assert len(report["target_extras"]) == 20
    assert plans and all("SEARCH" in detail for detail in plans)
    with database(config.state_dir, readonly=True) as db:
        assert db.execute("SELECT COUNT(*) FROM report_extras WHERE report_id=?", (report["id"],)).fetchone()[0] == 80


def interrupt_scan(config, *, completed_directory=False):
    import migraflow.planner as planner
    original = planner.os.scandir
    visited = []

    def scan(path):
        if completed_directory and Path(path) != config.source.path:
            visited.append(Path(path).name)
            if len(visited) == 2:
                raise KeyboardInterrupt
        children = original(path)
        if completed_directory:
            return children

        class Interrupted:
            def __enter__(self):
                def items():
                    for index, item in enumerate(children):
                        if index == 510:
                            raise KeyboardInterrupt
                        yield item
                return items()

            def __exit__(self, *args):
                children.close()

        return Interrupted()

    with patch("migraflow.planner.os.scandir", side_effect=scan), pytest.raises(KeyboardInterrupt):
        create_plan(config)
    with database(config.state_dir, readonly=True) as db:
        return summary(db)


def test_resume_partial_scan_replaces_unfinished_directory_without_duplicates(config):
    for index in range(520):
        (config.source.path / str(index)).write_bytes(b"x")
    interrupted = interrupt_scan(config)
    assert interrupted["status"] == "INTERRUPTED"
    assert interrupted["files"] == 500
    result = create_plan(config, resume_id=interrupted["id"])
    assert result["id"] == interrupted["id"]
    assert result["status"] == "PLANNED", result
    assert result["files"] == 520
    with database(config.state_dir, readonly=True) as db:
        assert db.execute("SELECT COUNT(*) FROM plans").fetchone()[0] == 1


def test_resume_scan_skips_completed_directories_and_rejects_source_changes(config):
    for name in ("a", "b"):
        folder = config.source.path / name
        folder.mkdir()
        (folder / "file").write_bytes(b"x")
    interrupted = interrupt_scan(config, completed_directory=True)
    import migraflow.planner as planner
    original = planner.os.scandir
    scanned = []

    def scan(path):
        scanned.append(Path(path).name)
        return original(path)

    with patch("migraflow.planner.os.scandir", side_effect=scan):
        result = create_plan(config, resume_id=interrupted["id"])
    assert result["status"] == "PLANNED", result
    assert len(scanned) == 1
    assert scanned[0] in ("a", "b")
    with pytest.raises(ConfigError, match="interrupted/failed"):
        create_plan(config, resume_id=result["id"])


def test_resume_scan_rejects_changed_file_and_legacy_checkpoint(config):
    for index in range(520):
        (config.source.path / str(index)).write_bytes(b"x")
    interrupted = interrupt_scan(config)
    with database(config.state_dir, readonly=True) as db:
        raw = db.execute("SELECT path FROM entries LIMIT 1").fetchone()[0]
    (config.source.path / os.fsdecode(raw)).write_bytes(b"changed")
    with pytest.raises(ConfigError, match="source file changed"):
        create_plan(config, resume_id=interrupted["id"])
    with database(config.state_dir) as db:
        db.execute("UPDATE directories SET generation='null'")
        db.commit()
    with pytest.raises(ConfigError, match="legacy"):
        create_plan(config, resume_id=interrupted["id"])


def test_metadata_config_and_cli_override(config, tmp_path):
    file = tmp_path / "migration.yml"
    file.write_text(f"source:\n  path: {config.source.path.as_posix()}\ndestination:\n  path: {config.destination.path.as_posix()}\nmetadata:\n  policy: posix\n")
    assert load_config(file).metadata_policy == "posix"
    assert load_config(file, metadata_policy="basic").metadata_policy == "basic"
    file.write_text(file.read_text().replace("posix", "unknown"))
    with pytest.raises(ConfigError, match="metadata.policy"):
        load_config(file)


@pytest.mark.skipif(os.name == "nt", reason="POSIX metadata requires Linux Python")
def test_posix_real_transfer_mode_owner_xattrs_and_tamper(config, rsync):
    config = replace(config, metadata_policy="posix")
    folder = config.source.path / "folder"
    folder.mkdir()
    source = folder / "file"
    source.write_bytes(b"contents")
    source.chmod(0o640)
    folder.chmod(0o550)
    os.setxattr(source, "user.migraflow-test", b"attribute")
    before = capture(source), capture(folder)
    result = create_plan(config)
    assert result["status"] == "PLANNED", result
    result = execute(config, result["id"], allow_unprotected_source=True)
    assert result["status"] == "VERIFIED", result
    target = config.destination.path / "folder" / "file"
    assert capture(target) == before[0]
    assert capture(target.parent) == before[1]
    assert (capture(source), capture(folder)) == before
    os.setxattr(target, "user.migraflow-test", b"tampered")
    assert execute(config, result["id"], verify_only=True)["status"] == "VERIFY_FAILED"
    assert execute(config, result["id"], resume=True, allow_unprotected_source=True)["status"] == "VERIFIED"


@pytest.mark.skipif(os.name == "nt", reason="POSIX metadata requires Linux Python")
def test_posix_source_metadata_change_blocks_old_plan(config, rsync):
    config = replace(config, metadata_policy="posix")
    source = config.source.path / "file"
    source.write_bytes(b"contents")
    result = create_plan(config)
    source.chmod(0o400)
    with pytest.raises(VerificationError, match="differ from plan"):
        execute(config, result["id"], allow_unprotected_source=True)
    assert not (config.destination.path / "file").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX ACLs require Linux Python")
def test_posix_acl_and_readonly_directory_survive_multiple_batches(config, rsync):
    config = replace(config, metadata_policy="posix")
    folder = config.source.path / "readonly"
    folder.mkdir()
    for index in range(505):
        (folder / str(index)).write_bytes(b"x")
    source = folder / "0"
    acl = struct.pack("<I", 2) + b"".join(struct.pack("<HHI", tag, permission, uid) for tag, permission, uid in (
        (1, 6, 0xffffffff), (2, 4, 12345), (4, 0, 0xffffffff), (16, 4, 0xffffffff), (32, 0, 0xffffffff)))
    os.setxattr(source, "system.posix_acl_access", acl)
    folder.chmod(0o550)
    expected = capture(source), capture(folder)
    result = create_plan(config)
    result = execute(config, result["id"], allow_unprotected_source=True)
    assert result["status"] == "VERIFIED", result
    target = config.destination.path / "readonly" / "0"
    assert capture(target) == expected[0]
    assert capture(target.parent) == expected[1]
    assert (capture(source), capture(folder)) == expected


def test_cutover_reports_reverify_and_persist_extras_but_do_not_delete(config, rsync):
    (config.source.path / "file").write_bytes(b"content")
    result = create_plan(config)
    assert execute(config, result["id"], allow_unprotected_source=True)["status"] == "VERIFIED"
    extra = config.destination.path / "extra"
    extra.write_bytes(b"keep")
    report = cutover_report(config, result["id"], source_freeze="   ")
    assert report["status"] == "BLOCKED"
    assert report["checks"]["fresh_verification"] is True
    assert not report["checks"]["source_freeze_recorded"]
    assert report["target_extras_count"] == 1
    assert report["cutover_executed"] is False
    assert extra.read_bytes() == b"keep"
    with database(config.state_dir, readonly=True) as db:
        assert json.loads(db.execute("SELECT result FROM reports WHERE id=?", (report["id"],)).fetchone()[0]) == report
        assert db.execute("SELECT path FROM report_extras").fetchone()[0] == b"extra"
        assert summary(db, result["id"])["status"] == "VERIFIED"
    target = config.destination.path / "file"
    info = target.stat()
    target.write_bytes(b"Content")
    os.utime(target, ns=(info.st_atime_ns, info.st_mtime_ns))
    report = cutover_report(config, result["id"])
    assert report["status"] == "BLOCKED"
    assert not report["checks"]["fresh_verification"]


def test_watch_is_readonly_and_interrupt_does_not_cancel_job(config):
    result = create_plan(config)
    updates = []

    class Display:
        def __init__(self, *args):
            pass

        def __enter__(self):
            return lambda event, **values: updates.append(values)

        def __exit__(self, *args):
            pass

    with patch("migraflow.monitor.Dashboard", Display), patch("migraflow.monitor.time.sleep", side_effect=KeyboardInterrupt):
        with pytest.raises(KeyboardInterrupt):
            watch(config.state_dir, None, Console(file=StringIO(), force_terminal=True))
    assert updates[0]["result"]["id"] == result["id"]
    assert updates[0]["database_bytes"] > 0
    with database(config.state_dir, readonly=True) as db:
        assert summary(db) == result
    with pytest.raises(ConfigError, match="interactive"):
        watch(config.state_dir, None, Console(file=StringIO()))
    assert main(["status", "--state-dir", str(config.state_dir), "--watch", "--json"]) == 2


def test_watch_rate_excludes_work_completed_before_monitor_start(config):
    result = create_plan(config)
    result.update(files=100, verified_files=80, file_statuses={"VERIFY_FAILED": 2})
    output = StringIO()
    console = Console(file=output, width=140)
    with patch("migraflow.progress.time.monotonic", return_value=100):
        dashboard = Dashboard(console, False)
    with patch("migraflow.progress.time.monotonic", return_value=120):
        dashboard.update("watch", result=result, database_bytes=1000, state_free_bytes=1000000)
    result["verified_files"] = 90
    with patch("migraflow.progress.time.monotonic", return_value=130):
        dashboard.update("watch", result=result, database_bytes=1000, state_free_bytes=1000000)
        console.print(dashboard)
    assert "1.0 files/s" in output.getvalue()
    assert dashboard.data["errors"] == 2


def test_report_streams_full_extras_and_cancel_retains_verified_job(config, rsync):
    (config.source.path / "file").write_bytes(b"contents")
    result = create_plan(config)
    result = execute(config, result["id"], allow_unprotected_source=True)
    extra = config.destination.path / "unplanned"
    extra.mkdir()
    for index in range(501):
        (extra / str(index)).write_bytes(b"keep")
    events = []
    report = cutover_report(config, result["id"], progress=lambda kind, **values: events.append((kind, values)))
    assert report["target_extras_count"] == 502
    assert len(report["target_extras"]) == 20
    start = next(index for index, (kind, values) in enumerate(events)
                 if kind == "phase" and "전환 보고서" in values["phase"])
    assert sum(values["files"] for kind, values in events[start:] if kind == "processed") == 503
    with database(config.state_dir, readonly=True) as db:
        assert db.execute("SELECT COUNT(*) FROM report_extras WHERE report_id=?", (report["id"],)).fetchone()[0] == 502
    with patch("migraflow.reporting.os.scandir", side_effect=KeyboardInterrupt), pytest.raises(KeyboardInterrupt):
        cutover_report(config, result["id"])
    with database(config.state_dir, readonly=True) as db:
        assert summary(db, result["id"])["status"] == "VERIFIED"
        interrupted = json.loads(db.execute("SELECT result FROM reports ORDER BY created_at DESC LIMIT 1").fetchone()[0])
        assert interrupted["status"] == "BUILDING"
        assert interrupted["cutover_ready"] is False
