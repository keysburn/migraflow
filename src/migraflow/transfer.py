import errno
import hashlib
import json
import os
import stat
import time
from dataclasses import asdict
from pathlib import Path

from .config import ConfigError
from .locking import file_lock
from .preflight import _no_links, config_data, endpoint_identity, filesystem, preflight
from .rsync_adapter import RsyncError, check_rsync, transfer_batch
from .state import database, now, summary, writer_lock
from .verifier import VerificationError, file_generation, hash_file


def _identity(config, checked):
    for label in ("source", "destination"):
        if endpoint_identity(getattr(config, label)) != checked[label]:
            raise ConfigError("Endpoint identity changed since planning.")
    if config.source.path.stat().st_mtime_ns != checked.get("source_root_mtime_ns"):
        raise VerificationError("Source root changed since planning. Create a new plan.")


def _path(root, raw, mount):
    relative = Path(os.fsdecode(raw))
    if relative.anchor or ".." in relative.parts:
        raise ConfigError("Invalid relative path in plan.")
    path = root / relative
    _no_links(path)
    if filesystem(path) != mount:
        raise ConfigError("Entry crosses an unexpected mount.")
    return path


def _planned_source(config, row, checked):
    path = _path(config.source.path.resolve(), row["path"], checked["source"]["mount"])
    info = path.stat(follow_symlinks=False)
    if row["kind"] == "file":
        identity = file_generation(path)
        if (info.st_size, info.st_mtime_ns) != (row["size"], row["mtime_ns"]):
            raise VerificationError(f"Source changed since planning: {path}")
        return identity
    if not stat.S_ISDIR(info.st_mode) or info.st_mtime_ns != row["mtime_ns"]:
        raise VerificationError(f"Source directory changed since planning: {path}")
    return None


def _rows(connection, plan_id, *, directories=False):
    condition = " AND kind='directory' ORDER BY length(path) DESC" if directories else " ORDER BY path"
    cursor = connection.execute("SELECT * FROM entries WHERE plan_id=?" + condition, (plan_id,))
    while batch := cursor.fetchmany(500):
        yield batch


def _load_plan(connection, plan_id, config, checked):
    planned = summary(connection, plan_id)
    saved = planned["config"].copy()
    original = json.dumps(saved, sort_keys=True, ensure_ascii=True)
    current = config_data(config)
    # Retry scheduling can change on resume, but endpoint/hash policy cannot.
    saved.pop("retry", None)
    current.pop("retry", None)
    saved.setdefault("hash_algorithm", "sha256")
    if planned["fingerprint"] != hashlib.sha256(original.encode()).hexdigest() or saved != current:
        raise ConfigError("Configuration differs from the saved plan. Create a new plan.")
    _identity(config, planned["preflight"])
    if checked["source"] != planned["preflight"]["source"] or checked["destination"] != planned["preflight"]["destination"]:
        raise ConfigError("Endpoint identity differs from the saved plan.")
    return planned


def _status(connection, plan_id, value, error=None):
    connection.execute("UPDATE plans SET status=?, error=?, updated_at=? WHERE id=?", (value, error, now(), plan_id))
    connection.commit()


def _record(connection, plan_id, raw):
    return connection.execute("SELECT * FROM file_results WHERE plan_id=? AND path=?", (plan_id, raw)).fetchone()


def _target(config, row, checked, overwrite):
    target = _path(config.destination.path.resolve(), row["path"], checked["destination"]["mount"])
    if target.exists():
        info = target.stat(follow_symlinks=False)
        if row["kind"] == "file":
            if not stat.S_ISREG(info.st_mode) or info.st_nlink > 1:
                raise ConfigError("Destination file type or hardlink is unsafe.")
            if not overwrite:
                raise ConfigError("Destination files already exist. Review the plan and backup before --overwrite.")
        elif not stat.S_ISDIR(info.st_mode):
            raise ConfigError("Destination directory type conflicts with plan.")
    return target


def _hash_with_retry(connection, plan_id, config, path, expected=None):
    for attempt in range(1, config.retry.max_attempts + 1):
        try:
            return hash_file(path, config.hash_algorithm, expected)
        except OSError as exc:
            if not _retryable(exc) or attempt == config.retry.max_attempts:
                raise
            delay = min(config.retry.initial_delay_seconds * 2 ** (attempt - 1), config.retry.max_delay_seconds)
            phase = connection.execute("SELECT status FROM plans WHERE id=?", (plan_id,)).fetchone()[0]
            connection.execute("""INSERT INTO attempts(plan_id,batch,attempt,created_at,outcome,error,delay_seconds)
                VALUES(?,-1,?,?,'HASH_RETRY',?,?)""", (plan_id, attempt, now(), str(exc), delay))
            _status(connection, plan_id, "RETRY_WAIT", str(exc))
            time.sleep(delay)
            _status(connection, plan_id, phase)


def _hashes(connection, plan_id, config, row, recorded, checked, *, restore_times=False, source_hash=None):
    identity = _planned_source(config, row, checked)
    expected = json.loads(recorded["source_generation"])
    if identity != expected:
        raise VerificationError("Source generation changed. Create a new plan.")
    source = _path(config.source.path.resolve(), row["path"], checked["source"]["mount"])
    target = _target(config, row, checked, True)
    if source_hash is None:
        source_hash, _ = _hash_with_retry(connection, plan_id, config, source, expected)
    if recorded["source_hash"] and source_hash != recorded["source_hash"]:
        raise VerificationError("Source content changed. Create a new plan.")
    destination_hash, destination_generation = _hash_with_retry(connection, plan_id, config, target)
    if file_generation(source) != expected or file_generation(target) != destination_generation:
        raise VerificationError("File changed during verification.")
    if source_hash != destination_hash:
        raise VerificationError("Source/destination content hashes differ.")
    if restore_times and destination_generation[3] != row["mtime_ns"]:
        os.utime(target, ns=(target.stat().st_atime_ns, row["mtime_ns"]))
        destination_generation = file_generation(target)
    if destination_generation[2:4] != [row["size"], row["mtime_ns"]]:
        raise VerificationError("Destination size/mtime differs from planned source.")
    return source_hash, destination_hash


def _save_verified(connection, plan_id, row, hashes):
    connection.execute("""UPDATE file_results SET status='VERIFIED', source_hash=?,
        destination_hash=?, copied=1, error=NULL WHERE plan_id=? AND path=?""",
        (*hashes, plan_id, row["path"]))


def _verify_batch(connection, plan_id, config, checked, batch):
    failures = 0
    for row in batch:
        try:
            _planned_source(config, row, checked)
            target = _path(config.destination.path.resolve(), row["path"], checked["destination"]["mount"])
            if row["kind"] == "directory":
                if not target.is_dir() or target.stat().st_mtime_ns != row["mtime_ns"]:
                    raise VerificationError(f"Destination directory metadata differs: {target}")
                connection.execute("DELETE FROM file_results WHERE plan_id=? AND path=?", (plan_id, row["path"]))
                continue
            recorded = _record(connection, plan_id, row["path"])
            if recorded is None:
                raise VerificationError("No transfer generation exists for this file.")
            hashes = _hashes(connection, plan_id, config, row, recorded, checked)
            _save_verified(connection, plan_id, row, hashes)
        except (OSError, ConfigError, VerificationError) as exc:
            failures += 1
            connection.execute("""INSERT INTO file_results(plan_id,path,status,algorithm,source_generation,error)
                VALUES(?,?,'VERIFY_FAILED',?,'null',?) ON CONFLICT(plan_id,path)
                DO UPDATE SET status='VERIFY_FAILED',error=excluded.error""",
                (plan_id, row["path"], config.hash_algorithm, str(exc)))
    connection.execute("UPDATE plans SET updated_at=? WHERE id=?", (now(), plan_id))
    connection.commit()
    return failures


def _verify(connection, plan_id, config, checked):
    _status(connection, plan_id, "VERIFYING")
    failures = 0
    for batch in _rows(connection, plan_id):
        _identity(config, checked)
        failures += _verify_batch(connection, plan_id, config, checked, batch)
    _identity(config, checked)
    _status(connection, plan_id, "VERIFY_FAILED" if failures else "VERIFIED")


def _recover_batch(connection, plan_id, config, checked, batch, overwrite):
    pending = []
    reused = 0
    for row in batch:
        identity = _planned_source(config, row, checked)
        if row["kind"] == "directory":
            target = _target(config, row, checked, overwrite)
            if not target.exists():
                pending.append(row)
            continue
        recorded = _record(connection, plan_id, row["path"])
        target = _path(config.destination.path.resolve(), row["path"], checked["destination"]["mount"])
        if recorded is not None:
            if json.loads(recorded["source_generation"]) != identity:
                raise VerificationError("Source generation changed. Create a new plan.")
            # Re-read both contents: same-size/mtime tampering must not reuse a checkpoint.
            source_hash = None
            if recorded["source_hash"]:
                source_hash, _ = _hash_with_retry(connection, plan_id, config,
                    config.source.path.resolve() / os.fsdecode(row["path"]), identity)
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
                    reused += 1
                    continue
        _target(config, row, checked, overwrite or (recorded is not None and bool(recorded["copied"])))
        connection.execute("""INSERT INTO file_results(plan_id,path,status,algorithm,source_generation)
            VALUES(?,?,'QUEUED',?,?) ON CONFLICT(plan_id,path) DO UPDATE SET status='QUEUED',error=NULL""",
            (plan_id, row["path"], config.hash_algorithm, json.dumps(identity)))
        pending.append(row)
    connection.commit()
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
    parent = config.destination.path.resolve() / (relative.parent if row["kind"] == "file" else relative)
    partial = parent / name
    _no_links(partial)
    if filesystem(partial) != checked["destination"]["mount"]:
        raise ConfigError("Partial directory crosses a mount.")
    if partial.exists():
        if initial or not partial.is_dir():
            raise ConfigError("Unexpected partial directory already exists.")


def _transfer_with_retry(connection, plan_id, config, checked, batch, batch_id,
                         prefix, manifest, log, overwrite, partial_name):
    policy = config.retry
    pending = batch
    for attempt in range(1, policy.max_attempts + 1):
        _identity(config, checked)
        pending, _ = _recover_batch(connection, plan_id, config, checked, pending, overwrite)
        if not pending:
            return
        for row in pending:
            _partial_paths(config, checked, row, partial_name)
            if row["kind"] == "file":
                connection.execute("UPDATE file_results SET status='TRANSFERRING' WHERE plan_id=? AND path=?",
                                   (plan_id, row["path"]))
        connection.commit()
        try:
            transfer_batch(prefix, config.source.path.resolve(), config.destination.path.resolve(),
                           [row["path"] for row in pending], manifest, log,
                           overwrite=overwrite, partial_dir=partial_name)
        except OSError as exc:
            delay = min(policy.initial_delay_seconds * 2 ** (attempt - 1), policy.max_delay_seconds)
            again = _retryable(exc) and attempt < policy.max_attempts
            connection.execute("""INSERT INTO attempts(plan_id,batch,attempt,created_at,outcome,error,delay_seconds)
                VALUES(?,?,?,?,?,?,?)""",
                (plan_id, batch_id, attempt, now(), "RETRY" if again else "FAILED", str(exc), delay if again else 0))
            _status(connection, plan_id, "RETRY_WAIT" if again else "FAILED", str(exc))
            if not again:
                raise
            time.sleep(delay)
            _status(connection, plan_id, "RUNNING")
            continue
        for row in pending:
            _planned_source(config, row, checked)
            if row["kind"] == "file":
                target = _target(config, row, checked, True)
                info = target.stat()
                os.utime(target, ns=(info.st_atime_ns, row["mtime_ns"]))
                connection.execute("UPDATE file_results SET status='TRANSFERRED',copied=1 WHERE plan_id=? AND path=?",
                                   (plan_id, row["path"]))
        connection.execute("""INSERT INTO attempts(plan_id,batch,attempt,created_at,outcome)
            VALUES(?,?,?,?,'SUCCESS')""", (plan_id, batch_id, attempt, now()))
        connection.commit()
        if _verify_batch(connection, plan_id, config, checked, [row for row in pending if row["kind"] == "file"]):
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


def execute(config, plan_id, *, verify_only=False, resume=False, overwrite=False, allow_unprotected_source=False):
    if config.source.type != "local" or config.destination.type != "local":
        raise ConfigError("This transfer release supports local-to-local only.")
    checked = preflight(config)
    state = Path(checked["state_dir"])
    _check_state_files(state)
    if not (state / "migraflow.sqlite3").is_file():
        raise ConfigError("Create a plan before running or verifying it.")
    with writer_lock(state), database(state) as connection:
        _idle_worker(state)
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ConfigError("State database integrity check failed.")
        try:
            planned = _load_plan(connection, plan_id, config, checked)
        except VerificationError as exc:
            if not verify_only and not resume:
                raise
            connection.execute("UPDATE file_results SET status='UNVERIFIED', source_hash=NULL, destination_hash=NULL WHERE plan_id=?", (plan_id,))
            _status(connection, plan_id, "VERIFY_FAILED", str(exc))
            return summary(connection, plan_id)
        allowed = ("TRANSFER_COMPLETE", "VERIFIED", "VERIFY_FAILED") if verify_only else (
            "PLANNED", "RUNNING", "RECOVERING", "RETRY_WAIT", "TRANSFER_COMPLETE", "VERIFYING",
            "VERIFIED", "VERIFY_FAILED", "FAILED", "INTERRUPTED") if resume else ("PLANNED",)
        if planned["status"] not in allowed:
            raise ConfigError(f"Cannot execute plan in state {planned['status']}.")
        options = planned["preflight"].get("execution_options", {})
        if resume and options and not options.get("supervisor_version") and planned["status"] in (
                "RUNNING", "FAILED", "INTERRUPTED", "VERIFYING"):
            raise ConfigError("Legacy interrupted transfer has no worker lease. Confirm old processes exited and create a new plan.")
        overwrite = overwrite or (resume and options.get("overwrite", False))
        if not verify_only:
            if not checked["source"]["read_only_mount"] and not allow_unprotected_source:
                raise ConfigError("Source read-only protection is unproven. For isolated development tests use --allow-unprotected-source.")
            prefix = check_rsync()
            for batch in _rows(connection, plan_id):
                for row in batch:
                    _planned_source(config, row, checked)
                    if not resume:
                        _target(config, row, checked, overwrite)
            if not resume and checked["destination_free_bytes"] < planned["bytes"]:
                raise ConfigError("Insufficient destination space for temporary transfer files.")
        try:
            if verify_only:
                _verify(connection, plan_id, config, checked)
                return summary(connection, plan_id)
            options = {"overwrite": overwrite, "allow_unprotected_source": allow_unprotected_source,
                       "rsync_command": prefix, "started_at": now(), "supervisor_version": 1,
                       "retry": asdict(config.retry), "reused_files": 0}
            planned["preflight"]["execution_options"] = options
            connection.execute("UPDATE plans SET preflight=? WHERE id=?", (json.dumps(planned["preflight"]), plan_id))
            _status(connection, plan_id, "RECOVERING" if resume else "RUNNING")
            scratch = state / ("transfer-" + plan_id)
            _no_links(scratch)
            scratch.mkdir(exist_ok=resume)
            manifest, log = scratch / "manifest.bin", scratch / "rsync.log"
            for path in (manifest, log):
                _no_links(path)
                if path.exists() and path.stat().st_nlink > 1:
                    raise ConfigError("Transfer scratch files must not be hardlinks.")
            partial_name = ".migraflow-partial-" + plan_id
            if resume:
                needed = 0
                for batch in _rows(connection, plan_id):
                    _identity(config, checked)
                    pending, reused = _recover_batch(connection, plan_id, config, checked, batch, overwrite)
                    options["reused_files"] += reused
                    needed += sum(row["size"] for row in pending if row["kind"] == "file")
                    connection.execute("UPDATE plans SET preflight=? WHERE id=?", (json.dumps(planned["preflight"]), plan_id))
                    connection.commit()
                if checked["destination_free_bytes"] < needed:
                    raise ConfigError("Insufficient destination space for remaining files.")
            _status(connection, plan_id, "RUNNING")
            for batch_id, batch in enumerate(_rows(connection, plan_id), 1):
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
            for batch in _rows(connection, plan_id, directories=True):
                _identity(config, checked)
                for row in batch:
                    _planned_source(config, row, checked)
                    target = _target(config, row, checked, True)
                    if not target.is_dir():
                        raise VerificationError("Transferred directory is missing or replaced.")
                    os.utime(target, ns=(target.stat().st_atime_ns, row["mtime_ns"]))
            _status(connection, plan_id, "TRANSFER_COMPLETE")
            # Reverify the saved inventory. Completed checkpoints are retained until replaced.
            _verify(connection, plan_id, config, checked)
        except KeyboardInterrupt:
            connection.rollback()
            _status(connection, plan_id, "INTERRUPTED")
            raise
        except (OSError, ConfigError, VerificationError) as exc:
            connection.rollback()
            if isinstance(exc, VerificationError):
                connection.execute("UPDATE file_results SET status='UNVERIFIED' WHERE plan_id=? AND status='VERIFIED'", (plan_id,))
            _status(connection, plan_id, "VERIFY_FAILED" if isinstance(exc, VerificationError) else "FAILED", str(exc))
        return summary(connection, plan_id)
