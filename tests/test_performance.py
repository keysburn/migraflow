import stat
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from migraflow.config import Config, ConfigError, Endpoint
from migraflow.planner import create_plan
from migraflow.preflight import _no_links, _parse_mounts, filesystem, mounts
from migraflow.state import database, summary


def test_mount_lookup_reads_fresh_table_and_detects_remount():
    base = "1 0 8:1 / / rw - ext4 /dev/root rw\n10 1 0:42 / /mnt/nas ro - nfs4 nas:/data rw\n"
    changed = base.replace("10 1", "11 1").replace("nas:/data", "other:/data")
    _parse_mounts.cache_clear()
    with patch("migraflow.preflight.sys.platform", "linux"), patch.object(
            Path, "read_text", side_effect=[base, base, changed]) as read:
        first = filesystem(PurePosixPath("/mnt/nas/file"))
        second = filesystem(PurePosixPath("/mnt/nas/file"))
        third = filesystem(PurePosixPath("/mnt/nas/file"))
    assert read.call_count == 3
    assert first == second
    assert third["mount_id"] == "11"
    assert third["source"] == "other:/data"


def test_mount_prefix_boundary_nested_precedence_and_no_mutable_cache_leak():
    table = "1 0 8:1 / / rw - ext4 /dev/root rw\n10 1 0:42 / /mnt/nas ro - nfs4 nas:/data rw\n11 1 0:43 / /mnt/nas/nested rw - cifs //nas/nested rw\n"
    with patch("migraflow.preflight.sys.platform", "linux"), patch.object(Path, "read_text", return_value=table):
        assert filesystem(PurePosixPath("/mnt/nas/nested/file"))["source"] == "//nas/nested"
        assert filesystem(PurePosixPath("/mnt/nas-backup/file"))["fs"] == "ext4"
        current = mounts()
        current[0]["options"].append("bad")
        current[0]["source"] = "bad"
        assert "bad" not in filesystem(PurePosixPath("/mnt/nas/nested/file"))["options"]
        assert filesystem(PurePosixPath("/mnt/nas/nested/file"))["source"] == "//nas/nested"


@pytest.mark.parametrize("kind", ["symlink", "junction"])
def test_ancestor_links_are_rechecked_on_every_call(tmp_path, kind):
    path = tmp_path / "sub" / "file"
    ordinary = SimpleNamespace(st_mode=stat.S_IFDIR, st_reparse_tag=0)
    unsafe = SimpleNamespace(st_mode=stat.S_IFLNK if kind == "symlink" else stat.S_IFDIR,
                             st_reparse_tag=0 if kind == "symlink" else 0xA0000003)
    with patch.object(Path, "lstat", return_value=ordinary):
        _no_links(path)

    def replaced(part):
        return unsafe if part == path.parent else ordinary

    with patch.object(Path, "lstat", new=replaced), pytest.raises(ConfigError, match="Symlink/junction"):
        _no_links(path)


def test_link_check_permission_failure_is_not_cached_or_suppressed(tmp_path):
    with patch.object(Path, "lstat", side_effect=PermissionError("denied")), pytest.raises(PermissionError):
        _no_links(tmp_path / "file")


def test_summary_combined_aggregation_preserves_mixed_kinds_and_statuses(tmp_path):
    source, target = tmp_path / "source", tmp_path / "destination"
    source.mkdir()
    target.mkdir()
    config = Config(Endpoint("local", source), Endpoint("local", target), tmp_path / "state")
    plan = create_plan(config)
    with database(config.state_dir) as db:
        db.executemany("INSERT INTO entries VALUES(?,?,?,?,?,?,?,?)", [
            (plan["id"], b"a", b"a", "file", 10, 0, "COPY_NEW", None),
            (plan["id"], b"b", b"b", "directory", 0, 0, "COPY_NEW", None),
            (plan["id"], b"c", b"c", "file", 20, 0, "OTHER_ACTION", None),
        ])
        db.executemany("INSERT INTO file_results(plan_id,path,status,algorithm,source_generation) VALUES(?,?,?,'sha256','null')", [
            (plan["id"], b"a", "VERIFIED"), (plan["id"], b"c", "QUEUED")])
        db.commit()
        result = summary(db, plan["id"])
    assert result["counts"] == {"COPY_NEW": 2, "OTHER_ACTION": 1}
    assert result["files"] == 2
    assert result["bytes"] == 30
    assert result["verified_files"] == 1
    assert result["file_statuses"] == {"VERIFIED": 1, "QUEUED": 1}


def test_existing_v4_database_gets_indexes_without_changing_history(tmp_path):
    source, target = tmp_path / "source", tmp_path / "destination"
    source.mkdir()
    target.mkdir()
    config = Config(Endpoint("local", source), Endpoint("local", target), tmp_path / "state")
    plan = create_plan(config)
    with database(config.state_dir) as db:
        for name in ("result_status", "result_errors", "entry_directories", "attempt_outcomes"):
            db.execute("DROP INDEX " + name)
        db.execute("DROP TABLE report_extras")
        db.execute("DROP TABLE reports")
        db.execute("DROP TABLE entry_metadata")
        db.execute("ALTER TABLE directories DROP COLUMN generation")
        db.execute("PRAGMA user_version=4")
        db.commit()
    with database(config.state_dir) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 6
        assert summary(db, plan["id"])["status"] == "PLANNED"
        assert db.execute("SELECT generation FROM directories").fetchone()[0] == "null"
        query = "SELECT * FROM entries WHERE plan_id=? AND kind='directory' ORDER BY length(path) DESC"
        detail = " ".join(row[3] for row in db.execute("EXPLAIN QUERY PLAN " + query, (plan["id"],)))
        assert "entry_directories" in detail
        assert "TEMP B-TREE" not in detail
        query = "SELECT path,error FROM file_results WHERE plan_id=? AND error IS NOT NULL LIMIT 20"
        detail = " ".join(row[3] for row in db.execute("EXPLAIN QUERY PLAN " + query, (plan["id"],)))
        assert "result_errors" in detail
