import json
import os
import sqlite3
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .config import ConfigError
from .locking import file_lock


def now():
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def writer_lock(directory):
    """OS lock releases on process exit, including crashes; no stale PID cleanup."""
    directory.mkdir(parents=True, exist_ok=True)
    with ExitStack() as locks:
        try:
            locks.enter_context(file_lock(directory / "planner.lock"))
        except OSError as exc:
            raise ConfigError("Another planner is using this state directory.") from exc
        yield


@contextmanager
def database(directory, *, readonly=False):
    path = directory / "migraflow.sqlite3"
    if readonly and not path.is_file():
        raise ConfigError("No plan database exists in state_dir.")
    connection = sqlite3.connect(path.as_uri() + ("?mode=ro" if readonly else "?mode=rwc"),
                                 uri=True, autocommit=sqlite3.LEGACY_TRANSACTION_CONTROL)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA foreign_keys=ON")
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1, 2, 3):
            raise ConfigError("Unsupported state database schema version.")
        if not readonly:
            if connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] != "wal":
                raise ConfigError("Cannot enable SQLite WAL.")
            connection.execute("PRAGMA synchronous=FULL")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS plans (
                    id TEXT PRIMARY KEY, status TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    config TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    preflight TEXT NOT NULL, error TEXT
                );
                CREATE TABLE IF NOT EXISTS entries (
                    plan_id TEXT NOT NULL REFERENCES plans(id), path BLOB NOT NULL,
                    name_key BLOB NOT NULL, kind TEXT NOT NULL, size INTEGER NOT NULL,
                    mtime_ns INTEGER NOT NULL, action TEXT NOT NULL, detail TEXT,
                    PRIMARY KEY (plan_id, path)
                );
                CREATE INDEX IF NOT EXISTS entry_names ON entries(plan_id, name_key);
                CREATE INDEX IF NOT EXISTS entry_actions ON entries(plan_id, action);
                CREATE TABLE IF NOT EXISTS directories (
                    plan_id TEXT NOT NULL REFERENCES plans(id), path BLOB NOT NULL,
                    done INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (plan_id, path)
                );
                CREATE INDEX IF NOT EXISTS pending_dirs ON directories(plan_id, done);
                CREATE TABLE IF NOT EXISTS file_results (
                    plan_id TEXT NOT NULL REFERENCES plans(id), path BLOB NOT NULL,
                    status TEXT NOT NULL, algorithm TEXT NOT NULL, source_generation TEXT NOT NULL,
                    source_hash TEXT, destination_hash TEXT, error TEXT,
                    PRIMARY KEY (plan_id, path)
                );
                CREATE TABLE IF NOT EXISTS attempts (
                    id INTEGER PRIMARY KEY, plan_id TEXT NOT NULL REFERENCES plans(id),
                    batch INTEGER NOT NULL, attempt INTEGER NOT NULL, created_at TEXT NOT NULL,
                    outcome TEXT NOT NULL, error TEXT, delay_seconds REAL NOT NULL DEFAULT 0
                );
            """)
            columns = {item[1] for item in connection.execute("PRAGMA table_info(file_results)")}
            if "copied" not in columns:
                connection.execute("ALTER TABLE file_results ADD COLUMN copied INTEGER NOT NULL DEFAULT 0")
                connection.execute("UPDATE file_results SET copied=1 WHERE status IN ('TRANSFERRED','VERIFIED','VERIFY_FAILED','UNVERIFIED') AND source_generation<>'null'")
            connection.execute("PRAGMA user_version=3")
            connection.commit()
        yield connection
    finally:
        connection.close()


def summary(connection, plan_id=None):
    row = connection.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone() if plan_id else connection.execute(
        "SELECT * FROM plans ORDER BY created_at DESC LIMIT 1").fetchone()
    if row is None:
        raise ConfigError("Plan not found.")
    result = dict(row)
    result["config"] = json.loads(result["config"])
    result["hash_algorithm"] = result["config"].get("hash_algorithm", "sha256")
    result["preflight"] = json.loads(result["preflight"])
    result["reused_files"] = result["preflight"].get("execution_options", {}).get("reused_files", 0)
    if result["status"] not in ("PLANNED", "SCANNING", "BLOCKED"):
        result["warnings"] = [warning for warning in result["preflight"].get("warnings", [])
                              if not warning.startswith("Metadata planning only")]
    result["counts"] = dict(connection.execute(
        "SELECT action, COUNT(*) FROM entries WHERE plan_id=? GROUP BY action", (row["id"],)).fetchall())
    result["files"] = connection.execute(
        "SELECT COUNT(*) FROM entries WHERE plan_id=? AND kind='file'", (row["id"],)).fetchone()[0]
    result["bytes"] = connection.execute(
        "SELECT COALESCE(SUM(size),0) FROM entries WHERE plan_id=? AND kind='file'", (row["id"],)).fetchone()[0]
    result["issues"] = [{"path": os.fsdecode(item[0]), "detail": item[1]} for item in connection.execute(
        "SELECT path, detail FROM entries WHERE plan_id=? AND action='BLOCKED' LIMIT 20", (row["id"],))]
    result["transfer_ready"] = False
    result["cutover_ready"] = False
    if connection.execute("SELECT 1 FROM sqlite_master WHERE name='attempts'").fetchone():
        result["retry_count"] = connection.execute(
            "SELECT COUNT(*) FROM attempts WHERE plan_id=? AND outcome IN ('RETRY','HASH_RETRY')", (row["id"],)).fetchone()[0]
    if connection.execute("SELECT 1 FROM sqlite_master WHERE name='file_results'").fetchone():
        result["file_statuses"] = dict(connection.execute(
            "SELECT status, COUNT(*) FROM file_results WHERE plan_id=? GROUP BY status", (row["id"],)).fetchall())
        result["verified_files"] = connection.execute(
            "SELECT COUNT(*) FROM file_results WHERE plan_id=? AND status='VERIFIED'", (row["id"],)).fetchone()[0]
        result["issues"].extend({"path": os.fsdecode(item[0]), "detail": item[1]} for item in connection.execute(
            "SELECT path, error FROM file_results WHERE plan_id=? AND error IS NOT NULL LIMIT 20", (row["id"],)))
    return result
