import pytest

from migraflow.state import database, summary
from migraflow.state_stats import refresh_deletions


def seed(db):
    for job in ("base", "new"):
        db.execute("INSERT INTO plans VALUES(?,?,?,?,?,?,?,?)",
                   (job, "PLANNED", "now", "now", "{}", "fingerprint", "{}", None))
        db.executemany("INSERT INTO entries VALUES(?,?,?,?,?,?,?,?)", [
            (job, b"a", b"a", "file", 10, 0, "COPY_NEW", None),
            (job, b"b", b"b", "directory", 0, 0, "MKDIR", None)])
    db.execute("INSERT INTO plan_lineage VALUES('new','base',1)")
    refresh_deletions(db, "new")
    db.commit()


def test_counters_follow_upsert_delete_replace_and_rollback(tmp_path):
    with database(tmp_path) as db:
        seed(db)
        db.execute("""INSERT INTO file_results(plan_id,path,status,algorithm,source_generation)
                      VALUES('new',?,'VERIFIED','sha256','null')""", (b"a",))
        db.commit()
        assert summary(db, "new")["verified_files"] == 1
        db.execute("""INSERT INTO file_results(plan_id,path,status,algorithm,source_generation)
            VALUES('new',?,'UNVERIFIED','sha256','null') ON CONFLICT(plan_id,path) DO UPDATE SET status=excluded.status""", (b"a",))
        db.execute("UPDATE entries SET action='BLOCKED',size=20 WHERE plan_id='new' AND path=?", (b"a",))
        changed = summary(db, "new")
        assert changed["verified_files"] == 0 and changed["bytes"] == 20
        db.rollback()
        result = summary(db, "new")
        assert result["verified_files"] == 1 and result["bytes"] == 10
        db.execute("INSERT OR REPLACE INTO entries VALUES('new',?,?,'file',30,0,'COPY_NEW',NULL)", (b"a", b"a"))
        assert summary(db, "new")["bytes"] == 30
        db.execute("DELETE FROM file_results WHERE plan_id='new'")
        db.execute("DELETE FROM entries WHERE plan_id='new'")
        result = summary(db, "new")
        assert result["file_statuses"] == {} and result["counts"] == {} and result["bytes"] == 0


def test_deletion_cache_invalidates_both_inventories_and_lineage(tmp_path):
    with database(tmp_path) as db:
        seed(db)
        assert summary(db, "new")["deletion_candidates_count"] == 0
        db.execute("DELETE FROM entries WHERE plan_id='new' AND path=?", (b"a",))
        assert db.execute("SELECT * FROM deletion_stats").fetchone() is None
        assert summary(db, "new")["deletion_candidates"] == [{"path": "a", "kind": "file"}]
        refresh_deletions(db, "new")
        db.execute("DELETE FROM entries WHERE plan_id='base' AND path=?", (b"a",))
        assert summary(db, "new")["deletion_candidates_count"] == 0
        refresh_deletions(db, "new")
        db.execute("UPDATE plan_lineage SET scan_complete=0")
        assert db.execute("SELECT * FROM deletion_stats").fetchone() is None
        assert not summary(db, "new")["deletion_candidates_complete"]


def test_readonly_summary_uses_persisted_totals_without_inventory_aggregation(tmp_path):
    with database(tmp_path) as db:
        seed(db)
    queries = []
    with database(tmp_path, readonly=True) as db:
        db.set_trace_callback(queries.append)
        result = summary(db, "new")
        assert result["files"] == 1 and result["deletion_candidates_count"] == 0
    assert not any("GROUP BY" in sql or "NOT EXISTS" in sql for sql in queries)
    assert not any(sql.startswith(("INSERT", "UPDATE", "DELETE")) for sql in queries)


@pytest.mark.parametrize("remove_auxiliary_tables", [False, True])
def test_v5_readonly_fallback_and_writer_rebuild_preserve_results(tmp_path, remove_auxiliary_tables):
    with database(tmp_path) as db:
        seed(db)
        expected = summary(db, "new")
        db.execute("PRAGMA user_version=5")
        db.execute("UPDATE inventory_counts SET bytes=999")
        if remove_auxiliary_tables:
            for row in db.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall():
                db.execute('DROP TRIGGER "' + row[0] + '"')
            for name in ("inventory_counts", "verification_counts", "deletion_stats"):
                db.execute("DROP TABLE " + name)
        db.commit()
    with database(tmp_path, readonly=True) as db:
        assert summary(db, "new") == expected
        assert db.execute("PRAGMA user_version").fetchone()[0] == 5
    with database(tmp_path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 6
        assert summary(db, "new") == expected


def test_reader_sees_counters_and_inventory_at_same_committed_checkpoint(tmp_path):
    with database(tmp_path) as writer:
        seed(writer)
        with database(tmp_path, readonly=True) as reader:
            writer.execute("UPDATE entries SET size=42 WHERE plan_id='new' AND kind='file'")
            assert summary(reader, "new")["bytes"] == 10
            writer.commit()
            assert summary(reader, "new")["bytes"] == 42
