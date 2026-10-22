"""SSH filesystem operations used by the existing planner and transfer state machine."""
import errno
import json
import os
import select
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from contextvars import ContextVar
from functools import wraps
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

from .config import ConfigError, TimeoutPolicy
from .ssh_protocol import helper_command, ssh_arguments


_sessions = ContextVar("ssh_sessions", default=None)


def remote_scope(function):
    @wraps(function)
    def scoped(*args, **kwargs):
        if _sessions.get() is not None:
            return function(*args, **kwargs)
        sessions = {}
        token = _sessions.set(sessions)
        try:
            return function(*args, **kwargs)
        finally:
            for session in sessions.values():
                session.close()
            _sessions.reset(token)
    return scoped


class Session:
    def __init__(self, endpoint, *, read_only, timeouts=None):
        self.timeouts = timeouts or TimeoutPolicy()
        self.header = {"host": endpoint.host, "root": str(endpoint.path), "state": str(endpoint.remote_state_dir), "read_only": read_only}
        self.closed = True
        self.open()

    def open(self):
        self.error = tempfile.TemporaryFile()
        try:
            self.process = subprocess.Popen(ssh_arguments(self.header["host"]) + [helper_command("rpc")],
                                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.error, bufsize=0)
        except (OSError, ValueError) as exc:
            self.error.close()
            raise ConfigError("Cannot start SSH: " + str(exc)) from exc
        self.buffer = bytearray()
        self.closed = False
        try:
            self.process.stdin.write(json.dumps(self.header, ensure_ascii=True).encode("ascii") + b"\n")
            self.process.stdin.flush()
        except BrokenPipeError as exc:
            self.close()
            raise OSError(errno.ECONNRESET, "SSH disconnected before session initialization.") from exc

    def call(self, op, path=".", **kwargs):
        from .verifier import VerificationError
        from .progress import emit
        if self.closed:
            self.open()
        if op == "hash":
            kwargs["progress_interval"] = min(1, self.timeouts.hash_idle_seconds / 3)
        request = json.dumps(dict(op=op, path=str(path), **kwargs), ensure_ascii=True).encode("ascii") + b"\n"
        started = time.monotonic()
        idle = self.timeouts.hash_idle_seconds if op == "hash" else 60
        total = self.timeouts.hash_total_seconds if op == "hash" else None
        idle_deadline, progressed = started + idle, 0
        try:
            self.process.stdin.write(request)
            self.process.stdin.flush()
            while True:
                deadline = min(idle_deadline, started + total) if total is not None else idle_deadline
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.close()
                    raise OSError(errno.ETIMEDOUT, "SSH helper idle/total timeout; resume after checking the endpoint.")
                if b"\n" not in self.buffer:
                    if not select.select([self.process.stdout], [], [], remaining)[0]:
                        self.close()
                        raise OSError(errno.ETIMEDOUT, "SSH helper idle/total timeout; resume after checking the endpoint.")
                    chunk = os.read(self.process.stdout.fileno(), 65536)
                    if not chunk:
                        self.error.seek(0)
                        detail = self.error.read(8192).decode("utf-8", "replace")
                        self.close()
                        if any(message in detail for message in ("Connection reset", "Connection timed out", "Broken pipe", "Connection closed", "Connection refused", "No route to host")):
                            raise OSError(errno.ECONNRESET, "SSH connection lost: " + detail)
                        raise ConfigError("SSH helper disconnected. Check host key, key authentication and remote Python/rsync: " + detail)
                    self.buffer.extend(chunk)
                    if len(self.buffer) > 16 * 1024 * 1024:
                        raise ConfigError("SSH helper response exceeds the bounded batch limit.")
                    continue
                line, _, rest = self.buffer.partition(b"\n")
                self.buffer = bytearray(rest)
                response = json.loads(line)
                if "progress_bytes" not in response:
                    break
                count = response["progress_bytes"]
                if op != "hash" or type(count) is not int or count <= progressed:
                    self.close()
                    raise ConfigError("Invalid SSH hash progress response.")
                emit("hash_chunk", bytes=count - progressed)
                progressed = count
                idle_deadline = time.monotonic() + idle
        except BrokenPipeError as exc:
            self.close()
            raise OSError(errno.ECONNRESET, "SSH transport pipe closed.") from exc
        except json.JSONDecodeError as exc:
            raise ConfigError("SSH helper connection failed or returned invalid data.") from exc
        if "error" in response:
            if response["error"] == "Changed":
                raise VerificationError(response["message"])
            if response.get("errno") is not None:
                raise OSError(response["errno"], response["message"])
            raise ConfigError(response["message"])
        result = response["result"]
        if op == "hash":
            if progressed > result[1][2]:
                raise ConfigError("SSH hash progress exceeds file size.")
            emit("hash_chunk", bytes=result[1][2] - progressed)
        if op == "hello":
            if result["protocol"] != 1:
                raise ConfigError("Unsupported SSH helper protocol.")
            self.header["identity"] = result["identity"]
            self.header["lease_identity"] = result["lease_identity"]
        return result

    def close(self):
        if self.closed:
            return
        if self.process.poll() is None:
            self.process.stdin.close()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.process.stdout.close()
        self.error.close()
        self.closed = True


class RemotePath:
    def __init__(self, session, relative="."):
        self.session = session
        self.relative = PurePosixPath(relative)

    def __str__(self):
        return f"{self.session.header['host']}:{PurePosixPath(self.session.header['root']) / self.relative}"

    def __truediv__(self, value):
        return RemotePath(self.session, self.relative / str(value))

    def call(self, op, **kwargs):
        return self.session.call(op, self.relative, **kwargs)

    def stat(self, *, follow_symlinks=True):
        return SimpleNamespace(**self.call("stat"))

    def exists(self):
        try:
            self.stat()
            return True
        except FileNotFoundError:
            return False

    def is_dir(self):
        try:
            return stat.S_ISDIR(self.stat().st_mode)
        except FileNotFoundError:
            return False

    def is_junction(self):
        return False


def root_path(config, label, checked=None):
    endpoint = getattr(config, label)
    if endpoint.type != "rsync_ssh":
        return Path(checked[label]["path"]) if checked else endpoint.path
    if sys.platform != "linux" or os.name == "nt":
        raise ConfigError("SSH endpoints require the Python CLI inside Linux/WSL.")
    sessions = _sessions.get()
    if sessions is None:
        raise ConfigError("SSH operations require a managed session scope.")
    key = (endpoint, label == "source")
    if key not in sessions:
        sessions[key] = Session(endpoint, read_only=label == "source", timeouts=config.timeouts)
    return RemotePath(sessions[key])


def endpoint_root(endpoint):
    # endpoint_identity has no config argument; use the already scoped session if present.
    sessions = _sessions.get() or {}
    for (candidate, _), session in sessions.items():
        if candidate == endpoint:
            return RemotePath(session)
    raise ConfigError("SSH endpoint session was not initialized.")


def utime(path, *, ns):
    if isinstance(path, RemotePath):
        path.call("utime", ns=ns)
    else:
        os.utime(path, ns=ns)


class RemoteDirectory:
    def __init__(self, path):
        self.path = path

    def __enter__(self):
        self.identifier = self.path.call("open_dir")
        return self.items()

    def items(self):
        while batch := self.path.call("next_dir", directory=self.identifier):
            for item in batch:
                yield RemoteEntry(self.path / item["name"], item)

    def __exit__(self, exc_type, exc, traceback):
        if self.path.session.closed:
            return
        try:
            self.path.call("close_dir", directory=self.identifier)
        except (OSError, ConfigError):
            if exc_type is None:
                raise


class RemoteEntry:
    def __init__(self, path, item):
        self.path, self.name, self.info = path, item["name"], SimpleNamespace(**item["stat"])

    def stat(self, *, follow_symlinks=True):
        return self.info

    def is_symlink(self):
        return stat.S_ISLNK(self.info.st_mode)


def scandir(path):
    return RemoteDirectory(path) if isinstance(path, RemotePath) else os.scandir(path)


def entry_path(entry):
    return entry.path if isinstance(entry.path, RemotePath) else Path(entry.path)


def preflight_ssh(config, *, test_destination_write=False):
    from .preflight import _inside, endpoint_identity, filesystem, state_path, _test_destination_write
    from .ssh_worker import machine_id
    if sum(getattr(config, label).type == "rsync_ssh" for label in ("source", "destination")) != 1:
        raise ConfigError("SSH push/pull requires exactly one local/mounted endpoint; two SSH endpoints are unsupported.")
    roots = {label: root_path(config, label) for label in ("source", "destination")}
    checks = {}
    info = {}
    for label, path in roots.items():
        if isinstance(path, RemotePath):
            info[label] = path.call("hello", posix=config.metadata_policy == "posix")
        checks[label] = endpoint_identity(getattr(config, label))
    state = state_path(config.state_dir)
    identities = []
    for label, path in roots.items():
        checked = checks[label]
        if isinstance(path, RemotePath):
            host = checked["machine_id"]
            lease = info[label]["lease_identity"]
            lease_mount = lease["mount"]
            lease_physical = Path(lease_mount["root"]) / Path(lease["path"]).relative_to(lease_mount["point"])
            identities.append((lease["machine_id"], lease["device"], lease_physical, lease_mount))
        else:
            host = machine_id()
            if _inside(state, path) or _inside(path, state):
                raise ConfigError("state_dir overlaps a local endpoint.")
            if not os.access(path, os.R_OK | os.X_OK):
                raise ConfigError("Local endpoint must be readable.")
            info[label] = {"readable": True, "writable": os.access(path, os.W_OK | os.X_OK), "free_bytes": shutil.disk_usage(path).free}
        if not info[label]["readable"]:
            raise ConfigError("SSH endpoint is not readable.")
        mount = checked["mount"]
        physical = Path(mount["root"]) / Path(checked["path"]).relative_to(mount["point"])
        identities.append((host, checked["device"], physical, mount))
    existing = state
    while not existing.exists():
        existing = existing.parent
    mount = filesystem(existing)
    physical = Path(mount["root"]) / state.relative_to(mount["point"])
    identities.append((machine_id(), existing.stat().st_dev, physical, mount))
    for index, left in enumerate(identities):
        for right in identities[index + 1:]:
            same_share = left[3]["fs"] in ("nfs", "nfs4", "cifs", "smb3") and left[3]["source"] == right[3]["source"]
            if (left[:2] == right[:2] or same_share) and (_inside(left[2], right[2]) or _inside(right[2], left[2])):
                raise ConfigError("Source, destination, coordinator state or remote lease overlap through SSH/bind/shared storage.")
    writable = info["destination"]["writable"] and not checks["destination"]["read_only_mount"]
    if test_destination_write:
        if not writable:
            raise ConfigError("Destination write test refused: destination is not writable.")
        target = roots["destination"]
        target.call("write_test") if isinstance(target, RemotePath) else _test_destination_write(target)
    protected = checks["source"]["read_only_mount"]
    warnings = ["SSH host keys and key authentication are enforced; use a stable source snapshot."]
    if not protected:
        warnings.append("Source read-only protection is unproven; enforce it before transfer.")
    if config.hash_algorithm == "md5":
        warnings.append("MD5 selected: collision resistance is weaker than SHA-256.")
    return {**checks, "state_dir": str(state), "source_root_mtime_ns": roots["source"].stat().st_mtime_ns,
            "destination_free_bytes": info["destination"]["free_bytes"], "destination_write_permission": writable,
            "destination_write_test": "passed" if test_destination_write else "not_requested",
            "source_protection": "read-only mount" if protected else "unproven", "hash_algorithm": config.hash_algorithm,
            "metadata_policy": config.metadata_policy, "transfer_ready": False, "warnings": warnings}
