import hashlib
import json
import os
import stat
import unicodedata
import uuid
from pathlib import Path

from .config import ConfigError
from .preflight import _no_links, config_data, endpoint_identity, filesystem, preflight
from .state import database, now, summary, writer_lock


def _target_action(source_info, kind, target, expected_mount):
    _no_links(target)
    if filesystem(target) != expected_mount:
        raise ConfigError("Destination crosses an unexpected nested mount.")
    try:
        target_info = target.stat(follow_symlinks=False)
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


def _scan(connection, plan_id, config, checked):
    source = Path(checked["source"]["path"])
    destination = Path(checked["destination"]["path"])
    connection.execute("INSERT INTO directories(plan_id,path) VALUES(?,?)", (plan_id, b""))
    connection.commit()
    while pending := connection.execute(
            "SELECT path FROM directories WHERE plan_id=? AND done=0 LIMIT 1", (plan_id,)).fetchone():
        for label in ("source", "destination"):
            if endpoint_identity(getattr(config, label)) != checked[label]:
                raise ConfigError("Endpoint identity changed while scanning.")
        relative_dir = os.fsdecode(pending[0])
        directory = source / relative_dir
        _no_links(directory)
        if filesystem(directory) != checked["source"]["mount"]:
            raise ConfigError("Source crosses an unexpected nested mount.")
        count = 0
        with os.scandir(directory) as children:
            for entry in children:
                relative = Path(relative_dir) / entry.name
                raw = os.fsencode(str(relative))
                key = unicodedata.normalize("NFC", str(relative)).casefold().encode("utf-8", "surrogatepass")
                detail = None
                kind, size, mtime = "unsupported", 0, 0
                action = "BLOCKED"
                try:
                    info = entry.stat(follow_symlinks=False)
                    if entry.is_symlink() or Path(entry.path).is_junction():
                        detail = "Symlink/junction entries are not supported in this planning release."
                    elif stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode):
                        kind = "directory" if stat.S_ISDIR(info.st_mode) else "file"
                        size = info.st_size if kind == "file" else 0
                        mtime = info.st_mtime_ns
                        action, detail = _target_action(info, kind, destination / relative, checked["destination"]["mount"])
                    else:
                        detail = "Special file types are not supported."
                except (OSError, ConfigError) as exc:
                    detail = str(exc)
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
                if kind == "directory" and action != "BLOCKED":
                    connection.execute("INSERT INTO directories(plan_id,path) VALUES(?,?)", (plan_id, raw))
                count += 1
                if count % 500 == 0:
                    connection.execute("UPDATE plans SET updated_at=? WHERE id=?", (now(), plan_id))
                    connection.commit()
        connection.execute("UPDATE directories SET done=1 WHERE plan_id=? AND path=?", (plan_id, pending[0]))
        connection.execute("UPDATE plans SET updated_at=? WHERE id=?", (now(), plan_id))
        connection.commit()
    # Catch lost/replaced roots even when the source was empty.
    for label in ("source", "destination"):
        if endpoint_identity(getattr(config, label)) != checked[label]:
            raise ConfigError("Endpoint identity changed while scanning.")


def create_plan(config):
    checked = preflight(config)
    state = Path(checked["state_dir"])
    for name in ("planner.lock", "migraflow.sqlite3", "migraflow.sqlite3-wal", "migraflow.sqlite3-shm"):
        path = state / name
        _no_links(path)
        if path.exists() and path.stat().st_nlink > 1:
            raise ConfigError("State files must not be hardlinks to other files.")
    plan_id = uuid.uuid4().hex
    data = json.dumps(config_data(config), sort_keys=True, ensure_ascii=True)
    with writer_lock(state), database(state) as connection:
        connection.execute("INSERT INTO plans VALUES(?,?,?,?,?,?,?,NULL)",
                           (plan_id, "SCANNING", now(), now(), data,
                            hashlib.sha256(data.encode()).hexdigest(), json.dumps(checked)))
        connection.commit()
        try:
            _scan(connection, plan_id, config, checked)
            blocked = connection.execute(
                "SELECT COUNT(*) FROM entries WHERE plan_id=? AND action='BLOCKED'", (plan_id,)).fetchone()[0]
            connection.execute("UPDATE plans SET status=?, updated_at=? WHERE id=?",
                               ("BLOCKED" if blocked else "PLANNED", now(), plan_id))
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
