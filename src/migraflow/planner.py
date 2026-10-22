import hashlib
import json
import os
import stat
import unicodedata
import uuid
from pathlib import Path

from .ssh import root_path, scandir, entry_path, remote_scope
from .config import ConfigError
from .metadata import snapshot, validate as validate_metadata
from .preflight import _no_links, checked_stat, compatible_plan, config_data, endpoint_identity, filesystem, preflight
from .state import database, now, summary, writer_lock
from .state_stats import refresh_deletions
from .verifier import VerificationError


def _target_action(source_info, kind, target, expected_mount):
    try:
        target_info = checked_stat(target, expected_mount)
    except FileNotFoundError:
        return ("MKDIR" if kind == "directory" else "COPY_NEW"), None
    target_kind = "directory" if stat.S_ISDIR(target_info.st_mode) else "file" if stat.S_ISREG(target_info.st_mode) else "unsupported"
    if kind != target_kind:
        return "BLOCKED", "Source/destination entry types conflict."
    if kind == "directory":
        return "EXISTING_DIRECTORY", None
    if (source_info.st_size, source_info.st_mtime_ns) == (target_info.st_size, target_info.st_mtime_ns):
        return "QUICK_MATCH_UNVERIFIED", "Equal size/mtime is not content verification."
    return "COPY_REPLACE", "Existing destination file would be overwritten during a future transfer."


def _baseline(connection, baseline_id, config, checked):
    saved = summary(connection, baseline_id)
    if saved["status"] != "VERIFIED":
        raise ConfigError("Incremental baseline must be a VERIFIED plan.")
    if not compatible_plan(saved, config):
        raise ConfigError("Baseline endpoints/state/hash algorithm differ. Create a full plan without --baseline.")
    for label in ("source", "destination"):
        if saved["preflight"][label] != checked[label]:
            raise ConfigError("Baseline endpoint identity changed. Create a full plan without --baseline.")


def _stable_inventory(connection, plan_id, config, checked):
    if root_path(config, "source", checked).stat().st_mtime_ns != checked["source_root_mtime_ns"]:
        raise ConfigError("Source root changed while scanning; deletion candidates are unconfirmed.")
    cursor = connection.execute("SELECT path,mtime_ns FROM entries WHERE plan_id=? AND kind='directory'", (plan_id,))
    while batch := cursor.fetchmany(500):
        for row in batch:
            path = root_path(config, "source", checked) / os.fsdecode(row["path"])
            _no_links(path)
            if filesystem(path) != checked["source"]["mount"] or path.stat().st_mtime_ns != row["mtime_ns"]:
                raise ConfigError("Source directory changed while scanning; deletion candidates are unconfirmed.")
    for label in ("source", "destination"):
        if endpoint_identity(getattr(config, label)) != checked[label]:
            raise ConfigError("Endpoint identity changed while confirming incremental inventory.")
    if root_path(config, "source", checked).stat().st_mtime_ns != checked["source_root_mtime_ns"]:
        raise ConfigError("Source root changed while scanning; deletion candidates are unconfirmed.")


def _directory_generation(path):
    _no_links(path)
    info = path.stat(follow_symlinks=False)
    if not stat.S_ISDIR(info.st_mode):
        raise ConfigError("Scan checkpoint directory disappeared or changed type.")
    return json.dumps([info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns])


def _scan(connection, plan_id, config, checked, baseline_id=None, *, resume=False):
    source = root_path(config, "source", checked)
    destination = root_path(config, "destination", checked)
    if not resume:
        connection.execute("INSERT INTO directories(plan_id,path,generation) VALUES(?,?,?)",
                           (plan_id, b"", _directory_generation(source)))
        connection.commit()
    while pending := connection.execute(
            "SELECT path,generation FROM directories WHERE plan_id=? AND done=0 LIMIT 1", (plan_id,)).fetchone():
        for label in ("source", "destination"):
            if endpoint_identity(getattr(config, label)) != checked[label]:
                raise ConfigError("Endpoint identity changed while scanning.")
        relative_dir = os.fsdecode(pending[0])
        directory = source / relative_dir
        _no_links(directory)
        if filesystem(directory) != checked["source"]["mount"]:
            raise ConfigError("Source crosses an unexpected nested mount.")
        if _directory_generation(directory) != pending["generation"]:
            raise ConfigError("Source directory changed since scan checkpoint; create a new plan.")
        if resume:
            if pending["path"]:
                separator = os.fsencode(os.sep)
                lower, upper = pending["path"] + separator, pending["path"] + bytes([separator[0] + 1])
                for table in ("entries", "entry_metadata", "directories"):
                    connection.execute(f"DELETE FROM {table} WHERE plan_id=? AND path>=? AND path<?", (plan_id, lower, upper))
            else:
                connection.execute("DELETE FROM entries WHERE plan_id=?", (plan_id,))
                connection.execute("DELETE FROM entry_metadata WHERE plan_id=?", (plan_id,))
                connection.execute("DELETE FROM directories WHERE plan_id=? AND path<>?", (plan_id, b""))
            connection.commit()
        count = 0
        with scandir(directory) as children:
            for entry in children:
                relative = Path(relative_dir) / entry.name
                raw = os.fsencode(str(relative))
                key = unicodedata.normalize("NFC", str(relative)).casefold().encode("utf-8", "surrogatepass")
                detail = None
                kind, size, mtime = "unsupported", 0, 0
                action = "BLOCKED"
                metadata = None
                try:
                    info = entry.stat(follow_symlinks=False)
                    if entry.is_symlink() or entry_path(entry).is_junction():
                        detail = "Symlink/junction entries are not supported in this planning release."
                    elif stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode):
                        kind = "directory" if stat.S_ISDIR(info.st_mode) else "file"
                        if kind == "directory":
                            info = entry_path(entry).stat(follow_symlinks=False)
                        size = info.st_size if kind == "file" else 0
                        mtime = info.st_mtime_ns
                        action, detail = _target_action(info, kind, destination / relative, checked["destination"]["mount"])
                        if config.metadata_policy == "posix":
                            metadata = snapshot(entry_path(entry))
                        if baseline_id and kind == "file" and action != "BLOCKED":
                            previous = connection.execute("""SELECT e.size,e.mtime_ns FROM entries e JOIN file_results f
                                ON f.plan_id=e.plan_id AND f.path=e.path WHERE e.plan_id=? AND e.path=?
                                AND e.kind='file' AND f.status='VERIFIED' AND f.copied=1 AND f.algorithm=?
                                AND f.source_hash IS NOT NULL AND f.source_hash=f.destination_hash""",
                                (baseline_id, raw, config.hash_algorithm)).fetchone()
                            if previous is not None:
                                metadata_changed = False
                                if metadata is not None:
                                    old = connection.execute("SELECT source_json FROM entry_metadata WHERE plan_id=? AND path=?",
                                                             (baseline_id, raw)).fetchone()
                                    metadata_changed = old is None or old[0] != metadata
                                action = "RESTORE_MISSING" if action == "COPY_NEW" else (
                                    "SOURCE_CHANGED" if metadata_changed or (size, mtime) != (previous["size"], previous["mtime_ns"]) else "INCREMENTAL_CHECK")
                                detail = "Baseline file: current hashes decide reuse; differing existing content requires this round's --overwrite."
                    else:
                        detail = "Special file types are not supported."
                except (OSError, ConfigError, VerificationError) as exc:
                    action, detail, metadata = "BLOCKED", str(exc), None
                collision = connection.execute(
                    "SELECT path FROM entries WHERE plan_id=? AND name_key=? AND path<>? LIMIT 1",
                    (plan_id, key, raw)).fetchone()
                if collision:
                    action, detail = "BLOCKED", "Case/Unicode-normalization filename collision."
                    connection.execute(
                        "UPDATE entries SET action='BLOCKED', detail=? WHERE plan_id=? AND name_key=?",
                        (detail, plan_id, key))
                connection.execute("INSERT INTO entries VALUES(?,?,?,?,?,?,?,?)",
                                   (plan_id, raw, key, kind, size, mtime, action, detail))
                if metadata is not None:
                    connection.execute("INSERT INTO entry_metadata VALUES(?,?,?)", (plan_id, raw, metadata))
                if kind == "directory" and action != "BLOCKED":
                    connection.execute("INSERT INTO directories(plan_id,path,generation) VALUES(?,?,?)",
                                       (plan_id, raw, _directory_generation(entry_path(entry))))
                count += 1
                if count % 500 == 0:
                    connection.execute("UPDATE plans SET updated_at=? WHERE id=?", (now(), plan_id))
                    connection.commit()
        if _directory_generation(directory) != pending["generation"]:
            raise ConfigError("Source directory changed during scanning; create a new plan.")
        connection.execute("UPDATE directories SET done=1 WHERE plan_id=? AND path=?", (plan_id, pending[0]))
        connection.execute("UPDATE plans SET updated_at=? WHERE id=?", (now(), plan_id))
        connection.commit()
    # Catch lost/replaced roots even when the source was empty.
    for label in ("source", "destination"):
        if endpoint_identity(getattr(config, label)) != checked[label]:
            raise ConfigError("Endpoint identity changed while scanning.")


@remote_scope
def create_plan(config, baseline_id=None, *, resume_id=None):
    checked = preflight(config)
    state = Path(checked["state_dir"])
    for name in ("planner.lock", "migraflow.sqlite3", "migraflow.sqlite3-wal", "migraflow.sqlite3-shm"):
        path = state / name
        _no_links(path)
        if path.exists() and path.stat().st_nlink > 1:
            raise ConfigError("State files must not be hardlinks to other files.")
    plan_id = resume_id or uuid.uuid4().hex
    data = json.dumps(config_data(config), sort_keys=True, ensure_ascii=True)
    with writer_lock(state), database(state) as connection:
        if resume_id:
            previous = summary(connection, resume_id)
            if previous["status"] not in ("SCANNING", "INTERRUPTED", "FAILED"):
                raise ConfigError("Only an interrupted/failed scan can use plan --resume.")
            if previous["preflight"].get("execution_options"):
                raise ConfigError("This is a transfer checkpoint; use resume --job instead.")
            if not compatible_plan(previous, config):
                raise ConfigError("Scan configuration changed; create a new plan.")
            for label in ("source", "destination"):
                if previous["preflight"][label] != checked[label]:
                    raise ConfigError("Scan endpoint identity changed; create a new plan.")
            cursor = connection.execute("SELECT path,generation FROM directories WHERE plan_id=?", (plan_id,))
            found = False
            while batch := cursor.fetchmany(500):
                for row in batch:
                    found = True
                    path = root_path(config, "source", checked) / os.fsdecode(row["path"])
                    if filesystem(path) != checked["source"]["mount"] or _directory_generation(path) != row["generation"]:
                        raise ConfigError("Scan checkpoint source changed or is legacy; create a new plan.")
            if not found:
                raise ConfigError("No resumable scan checkpoint exists; create a new plan.")
            cursor = connection.execute("SELECT path,size,mtime_ns FROM entries WHERE plan_id=? AND kind='file'", (plan_id,))
            while batch := cursor.fetchmany(500):
                for row in batch:
                    path = root_path(config, "source", checked) / os.fsdecode(row["path"])
                    _no_links(path)
                    info = path.stat(follow_symlinks=False)
                    if not stat.S_ISREG(info.st_mode) or (info.st_size, info.st_mtime_ns) != (row["size"], row["mtime_ns"]):
                        raise ConfigError("Scanned source file changed; create a new plan.")
                    if config.metadata_policy == "posix":
                        saved_metadata = connection.execute("SELECT source_json FROM entry_metadata WHERE plan_id=? AND path=?",
                                                            (plan_id, row["path"])).fetchone()
                        validate_metadata(path, saved_metadata[0] if saved_metadata else None)
            baseline_id = previous.get("baseline_id")
        if baseline_id:
            _baseline(connection, baseline_id, config, checked)
        if resume_id:
            connection.execute("UPDATE plans SET status='SCANNING',error=NULL,updated_at=? WHERE id=?", (now(), plan_id))
        else:
            connection.execute("INSERT INTO plans VALUES(?,?,?,?,?,?,?,NULL)",
                               (plan_id, "SCANNING", now(), now(), data,
                                hashlib.sha256(data.encode()).hexdigest(), json.dumps(checked)))
            if baseline_id:
                connection.execute("INSERT INTO plan_lineage(plan_id,baseline_id) VALUES(?,?)", (plan_id, baseline_id))
        connection.commit()
        try:
            _scan(connection, plan_id, config, checked, baseline_id, **({"resume": True} if resume_id else {}))
            blocked = connection.execute(
                "SELECT COUNT(*) FROM entries WHERE plan_id=? AND action='BLOCKED'", (plan_id,)).fetchone()[0]
            if baseline_id and not blocked:
                _stable_inventory(connection, plan_id, config, checked)
            connection.execute("UPDATE plans SET status=?, updated_at=? WHERE id=?",
                               ("BLOCKED" if blocked else "PLANNED", now(), plan_id))
            if baseline_id and not blocked:
                connection.execute("UPDATE plan_lineage SET scan_complete=1 WHERE plan_id=?", (plan_id,))
                refresh_deletions(connection, plan_id)
            connection.commit()
        except (OSError, ConfigError) as exc:
            connection.rollback()
            connection.execute("UPDATE plans SET status='FAILED', updated_at=?, error=? WHERE id=?",
                               (now(), str(exc), plan_id))
            connection.commit()
        except KeyboardInterrupt:
            connection.rollback()
            connection.execute("UPDATE plans SET status='INTERRUPTED', updated_at=? WHERE id=?", (now(), plan_id))
            connection.commit()
            raise
        return summary(connection, plan_id)
