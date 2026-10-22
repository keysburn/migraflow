"""Read/verify readiness reports; never changes a service or deletes target data."""
import json
import os
import stat
import uuid
from pathlib import Path

from .ssh import root_path, scandir, entry_path
from .config import ConfigError
from .preflight import _no_links, filesystem
from .progress import emit
from .state import now, summary
from .transfer import _identity, execute


def cutover_report(config, plan_id, *, source_freeze=None, destination_freeze=None, backup=None,
                   approval=None, metadata_review=None, extras_review=None, progress=None):
    references = {key: value.strip() if value else None for key, value in {
        "source_freeze": source_freeze, "destination_freeze": destination_freeze, "backup": backup,
        "approval": approval, "metadata_review": metadata_review, "extras_review": extras_review}.items()}
    def build(connection, plan_id, config, checked):
        result = summary(connection, plan_id)
        report_id = uuid.uuid4().hex
        created = now()
        connection.execute("INSERT INTO reports VALUES(?,?,?,?)", (report_id, plan_id, created,
                           json.dumps({"status": "BUILDING", "cutover_ready": False, "cutover_executed": False})))
        connection.commit()
        extras = unsupported = inspected = 0
        complete = result["status"] == "VERIFIED"
        error = None
        if complete:
            emit("phase", phase="전환 보고서 · 대상 추가 항목 조사", total_files=None, total_bytes=0)
            connection.execute("PRAGMA temp_store=FILE")
            connection.execute("CREATE TEMP TABLE report_queue(path BLOB PRIMARY KEY,done INTEGER NOT NULL DEFAULT 0)")
            connection.execute("CREATE INDEX report_queue_pending ON report_queue(done)")
            connection.execute("INSERT INTO report_queue(path) VALUES(?)", (b"",))
            connection.commit()
            try:
                while row := connection.execute("SELECT path FROM report_queue WHERE done=0 LIMIT 1").fetchone():
                    _identity(config, checked)
                    relative = os.fsdecode(row["path"])
                    directory = root_path(config, "destination", checked) / relative
                    _no_links(directory)
                    if filesystem(directory) != checked["destination"]["mount"]:
                        raise ConfigError("Report target crosses a nested mount.")
                    before = directory.stat()
                    directory_count = 0
                    with scandir(directory) as children:
                        for child in children:
                            path = Path(relative) / child.name
                            raw = os.fsencode(str(path))
                            info = child.stat(follow_symlinks=False)
                            kind = "directory" if stat.S_ISDIR(info.st_mode) else "file" if stat.S_ISREG(info.st_mode) else "unsupported"
                            if child.is_symlink() or entry_path(child).is_junction():
                                kind = "unsupported"
                            unsupported += kind == "unsupported"
                            if not connection.execute("SELECT 1 FROM entries WHERE plan_id=? AND path=?", (plan_id, raw)).fetchone():
                                extras += 1
                                connection.execute("INSERT INTO report_extras VALUES(?,?,?)", (report_id, raw, kind))
                            if kind == "directory":
                                connection.execute("INSERT INTO report_queue(path) VALUES(?)", (raw,))
                            inspected += 1
                            directory_count += 1
                            if inspected % 500 == 0:
                                connection.commit()
                    after = directory.stat()
                    if (before.st_dev, before.st_ino, before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_mtime_ns):
                        raise ConfigError("Target directory changed during readiness inventory.")
                    connection.execute("UPDATE report_queue SET done=1 WHERE path=?", (row["path"],))
                    connection.commit()
                    emit("processed", files=directory_count, bytes=0)
                _identity(config, checked)
            except (OSError, ConfigError) as exc:
                complete, error = False, str(exc)
            finally:
                connection.execute("DROP TABLE report_queue")
                connection.commit()
        checks = {"fresh_verification": result["status"] == "VERIFIED", "target_inventory_complete": complete,
                  "no_unsupported_entries": unsupported == 0, "source_read_only_mount": checked["source"]["read_only_mount"],
                  "source_freeze_recorded": bool(references["source_freeze"]), "destination_freeze_recorded": bool(references["destination_freeze"]),
                  "backup_recorded": bool(references["backup"]), "approval_recorded": bool(references["approval"]),
                  "metadata_scope_reviewed": bool(references["metadata_review"]), "extra_entries_reviewed": extras == 0 or bool(references["extras_review"])}
        ready = all(checks.values())
        report = {"id": report_id, "job": plan_id, "status": "READY" if ready else "BLOCKED",
                  "created_at": created, "verification_status": result["status"], "files": result["files"],
                  "verified_files": result["verified_files"], "hash_algorithm": config.hash_algorithm,
                  "metadata_policy": config.metadata_policy, "checks": checks, "references": references,
                  "target_extras_count": extras, "unsupported_entries": unsupported,
                  "target_extras": [{"path": os.fsdecode(item["path"]), "kind": item["kind"]}
                     for item in connection.execute("SELECT path,kind FROM report_extras WHERE report_id=? ORDER BY path LIMIT 20", (report_id,))],
                  "cutover_ready": ready, "cutover_executed": False, "error": error,
                  "reference_validation": "Operator-provided references; external tickets and freeze enforcement are not checked."}
        connection.execute("UPDATE reports SET result=? WHERE id=?", (json.dumps(report, ensure_ascii=True), report_id))
        connection.commit()
        return report

    return execute(config, plan_id, verify_only=True, progress=progress, after_verify=build)
