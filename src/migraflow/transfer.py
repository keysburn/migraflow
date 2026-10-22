import errno
import json
import os
import stat
import time
from dataclasses import asdict
from pathlib import Path

from .ssh import root_path, utime, remote_scope
from .config import ConfigError
from .locking import file_lock
from .metadata import validate as validate_metadata
from .preflight import _no_links, checked_filesystem, checked_stat, compatible_plan, endpoint_identity, filesystem, preflight
from .progress import emit, observing
from .rsync_adapter import RsyncError, check_rsync, transfer_batch
from .state import database, now, summary, writer_lock
from .verifier import VerificationError, file_generation, generation, hash_file


def _identity(config, checked):
    _mount_guard(config, checked)
    for label in ("source", "destination"):
        if endpoint_identity(getattr(config, label)) != checked[label]:
            raise ConfigError("Endpoint identity changed since planning.")
    if root_path(config, "source", checked).stat().st_mtime_ns != checked.get("source_root_mtime_ns"):
        raise VerificationError("Source root changed since planning. Create a new plan.")


def _mount_guard(config, checked):
    for label in ("source", "destination"):
        endpoint = getattr(config, label)
        if endpoint.type in ("mounted", "rsync_ssh") and filesystem(root_path(config, label, checked)) != checked[label]["mount"]:
            raise ConfigError("NAS mount disappeared or changed. Restore the planned mount before resuming; remounts require a new plan.")


def _entry_path(root, raw):
    relative = Path(os.fsdecode(raw))
    if relative.anchor or ".." in relative.parts:
        raise ConfigError("Invalid relative path in plan.")
    return root / relative


def _path(root, raw, mount):
    path = _entry_path(root, raw)
    if checked_filesystem(path) != mount:
        raise ConfigError("Entry crosses an unexpected mount.")
    return path


def _planned_source(config, row, checked):
    path = _entry_path(root_path(config, "source", checked), row["path"])
    try:
        info = checked_stat(path, checked["source"]["mount"])
    except (FileNotFoundError, NotADirectoryError) as exc:
        raise VerificationError(f"Planned source disappeared: {path}. Create a new plan.") from exc
    if row["kind"] == "file":
        if not stat.S_ISREG(info.st_mode):
            raise VerificationError(f"Expected a regular file: {path}")
        identity = generation(info)
        if identity[2:4] != [row["size"], row["mtime_ns"]]:
            raise VerificationError(f"Source changed since planning: {path}")
        if config.metadata_policy == "posix":
            validate_metadata(path, row["source_metadata"])
        return identity
    if not stat.S_ISDIR(info.st_mode) or info.st_mtime_ns != row["mtime_ns"]:
        raise VerificationError(f"Source directory changed since planning: {path}")
    if config.metadata_policy == "posix":
        validate_metadata(path, row["source_metadata"])
    return None


def _rows(connection, plan_id, *, directories=False, metadata=False):
    condition = " AND kind='directory' ORDER BY length(path) DESC" if directories else " ORDER BY path"
    columns = "*,(SELECT source_json FROM entry_metadata m WHERE m.plan_id=entries.plan_id AND m.path=entries.path) AS source_metadata" if metadata else "*"
    cursor = connection.execute("SELECT " + columns + " FROM entries WHERE plan_id=?" + condition, (plan_id,))
    while batch := cursor.fetchmany(500):
        yield batch


def _load_plan(connection, plan_id, config, checked):
    planned = summary(connection, plan_id)
    if not compatible_plan(planned, config):
        raise ConfigError("Configuration differs from the saved plan. Create a new plan.")
    _identity(config, planned["preflight"])
    if checked["source"] != planned["preflight"]["source"] or checked["destination"] != planned["preflight"]["destination"]:
        raise ConfigError("Endpoint identity differs from the saved plan.")
    return planned


def _status(connection, plan_id, value, error=None):
    connection.execute("UPDATE plans SET status=?, error=?, updated_at=? WHERE id=?", (value, error, now(), plan_id))
    connection.commit()
    emit("status", status=value, error=error)
    emit("checkpoint", time=now())


def _invalidate_verification(connection, plan_id, error):
    connection.execute("UPDATE file_results SET status='UNVERIFIED', source_hash=NULL, destination_hash=NULL WHERE plan_id=?", (plan_id,))
    _status(connection, plan_id, "VERIFY_FAILED", str(error))
    emit("checkpoint", verified=0)


def _record(connection, plan_id, raw):
    return connection.execute("SELECT * FROM file_results WHERE plan_id=? AND path=?", (plan_id, raw)).fetchone()


def _target(config, row, checked, overwrite):
    target = _entry_path(root_path(config, "destination", checked), row["path"])
    try:
        info = checked_stat(target, checked["destination"]["mount"])
    except FileNotFoundError:
        info = None
    if info is not None:
        if row["kind"] == "file":
            if not stat.S_ISREG(info.st_mode) or info.st_nlink > 1:
                raise ConfigError("Destination file type or hardlink is unsafe.")
            if not overwrite:
                raise ConfigError("Destination files already exist. Review the plan and backup before --overwrite.")
        elif not stat.S_ISDIR(info.st_mode):
            raise ConfigError("Destination directory type conflicts with plan.")
    return target


def _hash_with_retry(connection, plan_id, config, path, expected=None, *, checked):
    for attempt in range(1, config.retry.max_attempts + 1):
        _mount_guard(config, checked)
        try:
            result = hash_file(path, config.hash_algorithm, expected)
            _mount_guard(config, checked)
            return result
        except OSError as exc:
            if not _retryable(exc) or attempt == config.retry.max_attempts:
                raise
            delay = min(config.retry.initial_delay_seconds * 2 ** (attempt - 1), config.retry.max_delay_seconds)
            phase = connection.execute("SELECT status FROM plans WHERE id=?", (plan_id,)).fetchone()[0]
            connection.execute("""INSERT INTO attempts(plan_id,batch,attempt,created_at,outcome,error,delay_seconds)
                VALUES(?,-1,?,?,'HASH_RETRY',?,?)""", (plan_id, attempt, now(), str(exc), delay))
            emit("error", error=str(exc), retry=True)
            _status(connection, plan_id, "RETRY_WAIT", str(exc))
            time.sleep(delay)
            _status(connection, plan_id, phase)


def _hashes(connection, plan_id, config, row, recorded, checked, *, restore_times=False, source_hash=None):
    identity = _planned_source(config, row, checked)
    expected = json.loads(recorded["source_generation"])
    if identity != expected:
        raise VerificationError("Source generation changed. Create a new plan.")
    source = _path(root_path(config, "source", checked), row["path"], checked["source"]["mount"])
    target = _target(config, row, checked, True)
    if source_hash is None:
        source_hash, _ = _hash_with_retry(connection, plan_id, config, source, expected, checked=checked)
    if recorded["source_hash"] and source_hash != recorded["source_hash"]:
        raise VerificationError("Source content changed. Create a new plan.")
    destination_hash, destination_generation = _hash_with_retry(connection, plan_id, config, target, checked=checked)
    if file_generation(source) != expected or file_generation(target) != destination_generation:
        raise VerificationError("File changed during verification.")
    if source_hash != destination_hash:
        raise VerificationError("Source/destination content hashes differ.")
    if restore_times and destination_generation[3] != row["mtime_ns"]:
        utime(target, ns=(target.stat().st_atime_ns, row["mtime_ns"]))
        destination_generation = file_generation(target)
    if destination_generation[2:4] != [row["size"], row["mtime_ns"]]:
        raise VerificationError("Destination size/mtime differs from planned source.")
    if config.metadata_policy == "posix":
        validate_metadata(target, row["source_metadata"])
    return source_hash, destination_hash


def _save_verified(connection, plan_id, row, hashes):
    connection.execute("""UPDATE file_results SET status='VERIFIED', source_hash=?,
        destination_hash=?, copied=1, error=NULL WHERE plan_id=? AND path=?""",
        (*hashes, plan_id, row["path"]))


def _verify_batch(connection, plan_id, config, checked, batch):
    failures = 0
    verified_delta = 0
    for row in batch:
        _mount_guard(config, checked)
        recorded = _record(connection, plan_id, row["path"]) if row["kind"] == "file" else None
        try:
            _planned_source(config, row, checked)
            target = _path(root_path(config, "destination", checked), row["path"], checked["destination"]["mount"])
            if row["kind"] == "directory":
                if not target.is_dir() or target.stat().st_mtime_ns != row["mtime_ns"]:
                    raise VerificationError(f"Destination directory metadata differs: {target}")
                if config.metadata_policy == "posix":
                    validate_metadata(target, row["source_metadata"])
                connection.execute("DELETE FROM file_results WHERE plan_id=? AND path=?", (plan_id, row["path"]))
                continue
            if recorded is None:
                raise VerificationError("No transfer generation exists for this file.")
            hashes = _hashes(connection, plan_id, config, row, recorded, checked)
            _save_verified(connection, plan_id, row, hashes)
            verified_delta += recorded["status"] != "VERIFIED"
            emit("processed", files=1, bytes=row["size"], successes=1)
        except (OSError, ConfigError, VerificationError) as exc:
            failures += 1
            emit("error", error=str(exc))
            if row["kind"] == "file":
                verified_delta -= recorded is not None and recorded["status"] == "VERIFIED"
                emit("processed", files=1, bytes=row["size"], failures=1)
            connection.execute("""INSERT INTO file_results(plan_id,path,status,algorithm,source_generation,error)
                VALUES(?,?,'VERIFY_FAILED',?,'null',?) ON CONFLICT(plan_id,path)
                DO UPDATE SET status='VERIFY_FAILED',error=excluded.error""",
                (plan_id, row["path"], config.hash_algorithm, str(exc)))
    connection.execute("UPDATE plans SET updated_at=? WHERE id=?", (now(), plan_id))
    connection.commit()
    emit("checkpoint", delta=verified_delta, time=now())
    return failures


def _verify(connection, plan_id, config, checked):
    _status(connection, plan_id, "VERIFYING")
    totals = connection.execute("SELECT COUNT(*), COALESCE(SUM(size),0) FROM entries WHERE plan_id=? AND kind='file'",
                                (plan_id,)).fetchone()
    emit("phase", phase="전체 내용 검증", total_files=totals[0], total_bytes=totals[1])
    failures = 0
    for batch in _rows(connection, plan_id, metadata=config.metadata_policy == "posix"):
        _identity(config, checked)
        failures += _verify_batch(connection, plan_id, config, checked, batch)
    _identity(config, checked)
    _status(connection, plan_id, "VERIFY_FAILED" if failures else "VERIFIED")


def _recover_batch(connection, plan_id, config, checked, batch, overwrite):
    pending = []
    reused = 0
    verified_delta = 0
    for row in batch:
        _mount_guard(config, checked)
        identity = _planned_source(config, row, checked)
        if row["kind"] == "directory":
            target = _target(config, row, checked, overwrite)
            if not target.exists():
                pending.append(row)
            continue
        recorded = _record(connection, plan_id, row["path"])
        target = _path(root_path(config, "destination", checked), row["path"], checked["destination"]["mount"])
        if recorded is not None:
            if json.loads(recorded["source_generation"]) != identity:
                raise VerificationError("Source generation changed. Create a new plan.")
            # Re-read both contents: same-size/mtime tampering must not reuse a checkpoint.
            source_hash = None
            if recorded["source_hash"]:
                source_hash, _ = _hash_with_retry(connection, plan_id, config,
                    root_path(config, "source", checked) / os.fsdecode(row["path"]), identity, checked=checked)
                if source_hash != recorded["source_hash"]:
                    raise VerificationError("Source content changed. Create a new plan.")
            if target.exists():
                try:
                    hashes = _hashes(connection, plan_id, config, row, recorded, checked,
                                     restore_times=True, source_hash=source_hash)
                except VerificationError:
                    _target(config, row, checked, overwrite or bool(recorded["copied"]))
                else:
                    _save_verified(connection, plan_id, row, hashes)
                    verified_delta += recorded["status"] != "VERIFIED"
                    reused += 1
                    continue
        _target(config, row, checked, overwrite or (recorded is not None and bool(recorded["copied"])))
        verified_delta -= recorded is not None and recorded["status"] == "VERIFIED"
        connection.execute("""INSERT INTO file_results(plan_id,path,status,algorithm,source_generation)
            VALUES(?,?,'QUEUED',?,?) ON CONFLICT(plan_id,path) DO UPDATE SET status='QUEUED',error=NULL""",
            (plan_id, row["path"], config.hash_algorithm, json.dumps(identity)))
        pending.append(row)
    connection.commit()
    emit("checkpoint", delta=verified_delta, time=now())
    return pending, reused


def _retryable(exc):
    if isinstance(exc, RsyncError):
        return exc.retryable
    return isinstance(exc, OSError) and exc.errno in {
        errno.EAGAIN, errno.EINTR, errno.ETIMEDOUT, errno.ECONNRESET,
        errno.ECONNABORTED, errno.EPIPE, errno.ENETUNREACH, errno.EHOSTUNREACH,
    }


def _partial_paths(config, checked, row, name, *, initial=False):
    relative = Path(os.fsdecode(row["path"]))
    if name in relative.parts:
        raise ConfigError("Source collides with this job's reserved partial directory.")
    parent = root_path(config, "destination", checked) / (relative.parent if row["kind"] == "file" else relative)
    partial = parent / name
    _no_links(partial)
    if filesystem(partial) != checked["destination"]["mount"]:
        raise ConfigError("Partial directory crosses a mount.")
    if partial.exists():
        if initial or not partial.is_dir():
            raise ConfigError("Unexpected partial directory already exists.")


def _transfer_with_retry(connection, plan_id, config, checked, batch, batch_id,
                         prefix, manifest, log, overwrite, partial_name, *, force_directories=False):
    policy = config.retry
    pending = batch
    for attempt in range(1, policy.max_attempts + 1):
        _identity(config, checked)
        emit("phase", phase=f"배치 {batch_id} · 전송 전 점검 (시도 {attempt})",
             total_files=sum(row["kind"] == "file" for row in pending),
             total_bytes=sum(row["size"] for row in pending))
        pending, _ = _recover_batch(connection, plan_id, config, checked, pending, overwrite)
        if force_directories:
            pending = batch
        if not pending:
            return
        emit("phase", phase=f"배치 {batch_id} · rsync 전송 (시도 {attempt})",
             total_files=sum(row["kind"] == "file" for row in pending),
             total_bytes=sum(row["size"] for row in pending))
        for row in pending:
            _partial_paths(config, checked, row, partial_name)
            if row["kind"] == "file":
                connection.execute("UPDATE file_results SET status='TRANSFERRING' WHERE plan_id=? AND path=?",
                                   (plan_id, row["path"]))
        connection.commit()
        try:
            mounted = config.source.type == "mounted" or config.destination.type == "mounted"
            extra = {"guard": lambda: _mount_guard(config, checked), "pin_roots": True} if mounted else {}
            if config.metadata_policy == "posix":
                extra.update(preserve_metadata=True, final_directories=force_directories)
            extra["timeouts"] = config.timeouts
            transfer_batch(prefix, root_path(config, "source", checked), root_path(config, "destination", checked),
                           [row["path"] for row in pending], manifest, log,
                           overwrite=overwrite, partial_dir=partial_name,
                           **extra)
        except OSError as exc:
            delay = min(policy.initial_delay_seconds * 2 ** (attempt - 1), policy.max_delay_seconds)
            again = _retryable(exc) and attempt < policy.max_attempts
            connection.execute("""INSERT INTO attempts(plan_id,batch,attempt,created_at,outcome,error,delay_seconds)
                VALUES(?,?,?,?,?,?,?)""",
                (plan_id, batch_id, attempt, now(), "RETRY" if again else "FAILED", str(exc), delay if again else 0))
            emit("error", error=str(exc), retry=again)
            _status(connection, plan_id, "RETRY_WAIT" if again else "FAILED", str(exc))
            if not again:
                raise
            time.sleep(delay)
            _status(connection, plan_id, "RUNNING")
            continue
        _identity(config, checked)
        for row in pending:
            _mount_guard(config, checked)
            _planned_source(config, row, checked)
            if row["kind"] == "file":
                target = _target(config, row, checked, True)
                info = target.stat()
                utime(target, ns=(info.st_atime_ns, row["mtime_ns"]))
                connection.execute("UPDATE file_results SET status='TRANSFERRED',copied=1 WHERE plan_id=? AND path=?",
                                   (plan_id, row["path"]))
        connection.execute("""INSERT INTO attempts(plan_id,batch,attempt,created_at,outcome)
            VALUES(?,?,?,?,'SUCCESS')""", (plan_id, batch_id, attempt, now()))
        connection.commit()
        files = [row for row in pending if row["kind"] == "file"]
        emit("processed", files=len(files), bytes=sum(row["size"] for row in files))
        emit("phase", phase=f"배치 {batch_id} · 내용 검증", total_files=len(files),
             total_bytes=sum(row["size"] for row in files))
        if _verify_batch(connection, plan_id, config, checked, files):
            raise VerificationError("Batch content verification failed.")
        return


def _check_state_files(state):
    for name in ("planner.lock", "worker.lock", "migraflow.sqlite3", "migraflow.sqlite3-wal", "migraflow.sqlite3-shm"):
        path = state / name
        _no_links(path)
        if path.exists() and path.stat().st_nlink > 1:
            raise ConfigError("State files must not be hardlinks.")


def _idle_worker(state):
    deadline = time.monotonic() + 10
    while True:
        try:
            with file_lock(state / "worker.lock"):
                return
        except OSError:
            if time.monotonic() >= deadline:
                raise ConfigError("A previous transfer worker is still active. Resume after it exits.")
            time.sleep(0.1)


def _seed_incremental(connection, plan_id, config, checked):
    # Seed only prior verified paths. No previous hash/generation is reused as current proof.
    for batch in _rows(connection, plan_id, metadata=config.metadata_policy == "posix"):
        _identity(config, checked)
        for row in batch:
            if row["action"] not in ("INCREMENTAL_CHECK", "SOURCE_CHANGED", "RESTORE_MISSING"):
                continue
            if _record(connection, plan_id, row["path"]) is not None:
                continue
            generation = _planned_source(config, row, checked)
            connection.execute("""INSERT INTO file_results(plan_id,path,status,algorithm,source_generation)
                VALUES(?,?,'QUEUED',?,?)""", (plan_id, row["path"], config.hash_algorithm, json.dumps(generation)))
        connection.commit()


@remote_scope
def execute(config, plan_id, *, verify_only=False, resume=False, overwrite=False, allow_unprotected_source=False,
            progress=None, after_verify=None):
    with observing(progress):
        return _execute(config, plan_id, verify_only=verify_only, resume=resume, overwrite=overwrite,
                        allow_unprotected_source=allow_unprotected_source, after_verify=after_verify)


def _check_inventory(connection, plan_id, config, checked, *, resume, overwrite, incremental):
    for batch in _rows(connection, plan_id, metadata=config.metadata_policy == "posix"):
        for row in batch:
            _mount_guard(config, checked)
            _planned_source(config, row, checked)
            if not resume:
                _target(config, row, checked, overwrite or (incremental and row["action"] in
                        ("INCREMENTAL_CHECK", "SOURCE_CHANGED", "RESTORE_MISSING")))
        emit("processed", files=sum(row["kind"] == "file" for row in batch),
             bytes=sum(row["size"] for row in batch))


def _execute(config, plan_id, *, verify_only=False, resume=False, overwrite=False, allow_unprotected_source=False, after_verify=None):
    checked = preflight(config)
    state = Path(checked["state_dir"])
    _check_state_files(state)
    if not (state / "migraflow.sqlite3").is_file():
        raise ConfigError("Create a plan before running or verifying it.")
    with writer_lock(state), database(state) as connection:
        _idle_worker(state)
        for label in ("source", "destination"):
            if getattr(config, label).type == "rsync_ssh":
                root_path(config, label, checked).call("idle")
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ConfigError("State database integrity check failed.")
        try:
            planned = _load_plan(connection, plan_id, config, checked)
        except VerificationError as exc:
            if not verify_only and not resume:
                raise
            _invalidate_verification(connection, plan_id, exc)
            return summary(connection, plan_id)
        allowed = ("TRANSFER_COMPLETE", "VERIFIED", "VERIFY_FAILED") if verify_only else (
            "PLANNED", "RUNNING", "RECOVERING", "RETRY_WAIT", "TRANSFER_COMPLETE", "VERIFYING",
            "VERIFIED", "VERIFY_FAILED", "FAILED", "INTERRUPTED") if resume else ("PLANNED",)
        if planned["status"] not in allowed:
            raise ConfigError(f"Cannot execute plan in state {planned['status']}.")
        options = planned["preflight"].get("execution_options", {})
        incremental = planned.get("baseline_id") is not None
        emit("init", job=plan_id, status=planned["status"], retries=planned["retry_count"],
             planned_files=planned["files"], planned_bytes=planned["bytes"], verified=planned["verified_files"],
             source=str(config.source.path), destination=str(config.destination.path), algorithm=config.hash_algorithm,
             checkpoint=planned["updated_at"])
        if resume and options and not options.get("supervisor_version") and planned["status"] in (
                "RUNNING", "FAILED", "INTERRUPTED", "VERIFYING"):
            raise ConfigError("Legacy interrupted transfer has no worker lease. Confirm old processes exited and create a new plan.")
        overwrite = overwrite or (resume and options.get("overwrite", False))
        if not verify_only:
            if checked["destination"]["read_only_mount"]:
                raise ConfigError("Destination mount is read-only.")
            if not checked["source"]["read_only_mount"] and not allow_unprotected_source:
                raise ConfigError("Source read-only protection is unproven. For isolated development tests use --allow-unprotected-source.")
            prefix = check_rsync()
            emit("phase", phase="전송 전 경로·세대 점검", total_files=planned["files"], total_bytes=planned["bytes"])
            try:
                _check_inventory(connection, plan_id, config, checked, resume=resume,
                                 overwrite=overwrite, incremental=incremental)
            except VerificationError as exc:
                _invalidate_verification(connection, plan_id, exc)
                if not resume:
                    raise
                return summary(connection, plan_id)
            if not resume and not incremental and checked["destination_free_bytes"] < planned["bytes"]:
                raise ConfigError("Insufficient destination space for temporary transfer files.")
        verification_done = False
        try:
            if verify_only:
                _verify(connection, plan_id, config, checked)
                verification_done = True
                if after_verify is not None:
                    return after_verify(connection, plan_id, config, checked)
                return summary(connection, plan_id)
            options = {"overwrite": overwrite, "allow_unprotected_source": allow_unprotected_source,
                       "rsync_command": prefix, "started_at": now(), "supervisor_version": 1,
                       "retry": asdict(config.retry), "timeouts": asdict(config.timeouts), "reused_files": 0}
            planned["preflight"]["execution_options"] = options
            connection.execute("UPDATE plans SET preflight=? WHERE id=?", (json.dumps(planned["preflight"]), plan_id))
            _status(connection, plan_id, "RECOVERING" if resume or incremental else "RUNNING")
            scratch = state / ("transfer-" + plan_id)
            _no_links(scratch)
            scratch.mkdir(exist_ok=resume)
            manifest, log = scratch / "manifest.bin", scratch / "rsync.log"
            for path in (manifest, log):
                _no_links(path)
                if path.exists() and path.stat().st_nlink > 1:
                    raise ConfigError("Transfer scratch files must not be hardlinks.")
            partial_name = ".migraflow-partial-" + plan_id
            if incremental:
                _seed_incremental(connection, plan_id, config, checked)
            if resume or incremental:
                emit("phase", phase="증분 점검 · 양측 해시 재확인" if incremental and not resume else "재개 점검 · 양측 해시 재확인",
                     total_files=planned["files"], total_bytes=planned["bytes"])
                needed = 0
                for batch in _rows(connection, plan_id, metadata=config.metadata_policy == "posix"):
                    _identity(config, checked)
                    pending, reused = _recover_batch(connection, plan_id, config, checked, batch, overwrite)
                    options["reused_files"] += reused
                    emit("reuse", files=reused)
                    emit("processed", files=sum(row["kind"] == "file" for row in batch),
                         bytes=sum(row["size"] for row in batch))
                    needed += sum(row["size"] for row in pending if row["kind"] == "file")
                    connection.execute("UPDATE plans SET preflight=? WHERE id=?", (json.dumps(planned["preflight"]), plan_id))
                    connection.commit()
                if checked["destination_free_bytes"] < needed:
                    raise ConfigError("Insufficient destination space for remaining files.")
            _status(connection, plan_id, "RUNNING")
            for batch_id, batch in enumerate(_rows(connection, plan_id, metadata=config.metadata_policy == "posix"), 1):
                _identity(config, checked)
                pending = []
                for row in batch:
                    _partial_paths(config, checked, row, partial_name, initial=not resume)
                    recorded = _record(connection, plan_id, row["path"]) if row["kind"] == "file" else None
                    if recorded is not None and recorded["status"] == "VERIFIED":
                        continue
                    if row["kind"] == "directory" and _target(config, row, checked, overwrite).exists():
                        continue
                    pending.append(row)
                # Separate authorized repairs from new paths; never broaden overwrite to a mixed batch.
                for replace in (False, True):
                    group = []
                    for row in pending:
                        recorded = _record(connection, plan_id, row["path"]) if row["kind"] == "file" else None
                        can_replace = bool(overwrite or (recorded is not None and recorded["copied"]))
                        if can_replace == replace:
                            group.append(row)
                    if group:
                        _transfer_with_retry(connection, plan_id, config, checked, group, batch_id,
                                             prefix, manifest, log, replace, partial_name)
            emit("phase", phase="디렉터리 메타데이터 확인", total_files=None, total_bytes=0)
            for batch in _rows(connection, plan_id, directories=True, metadata=config.metadata_policy == "posix"):
                _identity(config, checked)
                if config.metadata_policy == "posix":
                    _transfer_with_retry(connection, plan_id, config, checked, batch, -1,
                                         prefix, manifest, log, True, partial_name, force_directories=True)
                for row in batch:
                    _planned_source(config, row, checked)
                    target = _target(config, row, checked, True)
                    if not target.is_dir():
                        raise VerificationError("Transferred directory is missing or replaced.")
                    utime(target, ns=(target.stat().st_atime_ns, row["mtime_ns"]))
            _status(connection, plan_id, "TRANSFER_COMPLETE")
            # Reverify the saved inventory. Completed checkpoints are retained until replaced.
            _verify(connection, plan_id, config, checked)
        except KeyboardInterrupt:
            connection.rollback()
            if not (verify_only and verification_done and after_verify is not None):
                _status(connection, plan_id, "INTERRUPTED")
            raise
        except (OSError, ConfigError, VerificationError) as exc:
            connection.rollback()
            if isinstance(exc, VerificationError):
                _invalidate_verification(connection, plan_id, exc)
            else:
                _status(connection, plan_id, "FAILED", str(exc))
        return summary(connection, plan_id)
