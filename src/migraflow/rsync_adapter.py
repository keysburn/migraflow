import os
import re
import json
import shlex
import stat
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from .config import ConfigError, TimeoutPolicy
from .progress import emit, observed
from .ssh import RemotePath
from .ssh_protocol import helper_command


_PROGRESS = re.compile(rb"^\s*(\d[\d,]{0,31})\s+(\d{1,3})%\s+([\d.]+[kKMGT]?B/s)\s+(\d{1,6}:\d{2}:\d{2})(?:\s+\([^\r\n]*\))?\s*$")


class ProgressLog:
    """Bounded log tail for display only; ignore unknown formats and read errors."""
    def __init__(self, path):
        self.path = path
        self.buffer = b""
        try:
            self.offset = path.stat().st_size if path.exists() else 0
        except OSError:
            self.offset = 0

    def poll(self):
        try:
            with self.path.open("rb") as log:
                log.seek(self.offset)
                chunk = log.read(4096)
                self.offset += len(chunk)
        except OSError:
            return
        parts = re.split(rb"[\r\n]", self.buffer + chunk)
        self.buffer = parts[-1][-4096:]
        for part in parts:
            match = _PROGRESS.fullmatch(part)
            if match and int(match[2]) <= 100:
                emit("rsync", bytes=int(match[1].replace(b",", b"")), percent=int(match[2]),
                     speed=match[3].decode("ascii"), time=match[4].decode("ascii"))


class RsyncError(OSError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.retryable = code in (10, 12, 30, 35)


def command_prefix():
    if os.name == "nt":
        binary = shutil.which("wsl.exe")
        if not binary:
            raise ConfigError("Windows transfer requires WSL with rsync installed in its default distribution.")
        return [binary, "--exec", "rsync"]
    binary = shutil.which("rsync")
    if not binary:
        raise ConfigError("Install rsync before running a transfer.")
    return [binary]


def check_rsync():
    prefix = command_prefix()
    try:
        probe = prefix + ["--version"]
        if os.name == "nt":
            probe = [prefix[0], "--exec", "python3", "-c",
                     "import subprocess; subprocess.run(['rsync', '--version'], check=True)"]
        result = subprocess.run(probe, capture_output=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ConfigError(f"Cannot start rsync: {exc}") from exc
    if result.returncode != 0:
        raise ConfigError("rsync is unavailable; on Windows check the default WSL distribution.")
    return prefix


def rsync_path(path):
    if os.name != "nt":
        return str(path)
    try:
        result = subprocess.run([shutil.which("wsl.exe"), "--exec", "wslpath", "-a", str(path)],
                                capture_output=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ConfigError(f"WSL path translation failed: {exc}") from exc
    if result.returncode != 0:
        raise ConfigError("WSL could not translate the local Windows path.")
    return result.stdout.decode("utf-8").strip()


def transfer_batch(prefix, source, destination, paths, manifest, log, *, overwrite=False, partial_dir=None,
                   guard=None, pin_roots=False, preserve_metadata=False, final_directories=False, timeouts=None):
    timeouts = timeouts or TimeoutPolicy()
    # Windows relative paths use backslashes; POSIX filenames may contain them.
    manifest.write_bytes(b"".join((path.replace(b"\\", b"/") if os.name == "nt" else path) + b"\0"
                                  for path in paths))
    policy = ["--timeout=" + str(timeouts.transfer_idle_seconds)]
    if not overwrite:
        policy += ["--ignore-existing"]
    telemetry = observed()
    reader = ProgressLog(log) if telemetry else None
    if telemetry:
        policy += ["--info=progress2,name0", "--outbuf=N"]
    if partial_dir:
        policy += ["--partial-dir=" + partial_dir, "--no-whole-file"]
    if preserve_metadata:
        if os.name == "nt":
            raise ConfigError("POSIX metadata transfer requires the Python CLI inside Linux/WSL.")
        policy += ["--perms", "--owner", "--group", "--numeric-ids", "--acls", "--xattrs"]
        if not final_directories:
            policy += ["--chmod=Du+rwx"]
    arguments = prefix + policy + ["--dirs", "--times", "--modify-window=-1", "--ignore-times", "--no-recursive", "--no-links",
                          "--from0", "--files-from=-"]
    remote = source if isinstance(source, RemotePath) else destination if isinstance(destination, RemotePath) else None
    if remote is not None:
        if os.name == "nt":
            raise ConfigError("SSH transfer requires Linux/WSL Python.")
        header = dict(remote.session.header, identity=remote.call("identity"))
        settings = manifest.with_suffix(".ssh.json")
        descriptor = os.open(settings, os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            info = os.fstat(file.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ConfigError("SSH transport settings must be a regular file without hardlinks.")
            os.ftruncate(file.fileno(), 0)
            json.dump(header, file, ensure_ascii=True)
        proxy = shlex.join([sys.executable, str(Path(__file__).with_name("ssh_proxy.py")), str(settings)])
        arguments += ["-s", "--rsh=" + proxy, "--rsync-path=" + helper_command("serve")]
    arguments += ["--", remote.session.header["host"] + ":." if isinstance(source, RemotePath) else rsync_path(source) + "/",
                  remote.session.header["host"] + ":." if isinstance(destination, RemotePath) else rsync_path(destination) + "/"]
    worker = Path(__file__).with_name("worker.py")
    helper = [sys.executable, str(worker), "--lock", str(manifest.parent.parent / "worker.lock"),
              "--manifest", str(manifest)]
    if timeouts.transfer_total_seconds is not None:
        helper += ["--timeout", str(timeouts.transfer_total_seconds + (20 if os.name == "nt" else 0))]
    if remote is not None:
        helper += ["--pin-index", "-1" if isinstance(source, RemotePath) else "-2"]
    elif pin_roots:
        if os.name == "nt":
            raise ConfigError("NAS transfer requires running the Python CLI inside Linux/WSL.")
        helper += ["--pin-roots"]
    if os.name == "nt":
        arguments = [prefix[0], "--exec", "python3", rsync_path(worker), "--manifest", rsync_path(manifest),
                     *(["--timeout", str(timeouts.transfer_total_seconds)] if timeouts.transfer_total_seconds is not None else []),
                     "--", *arguments[2:]]
        helper += ["--forward-lease"]
    helper += ["--", *arguments]
    with log.open("ab") as output:
        process = subprocess.Popen(helper, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=output)
        ready = []
        event = threading.Event()

        def read_ready():
            ready.append(process.stdout.readline())
            event.set()

        threading.Thread(target=read_ready, daemon=True).start()
        try:
            if not event.wait(30) or not ready or ready[0].strip() != b"READY":
                raise ConfigError("Transfer worker could not acquire its lease. See rsync.log.")
            if guard:
                guard()
            process.stdin.write(b"1")
            process.stdin.flush()
            deadline = time.monotonic() + timeouts.transfer_total_seconds + 40 if timeouts.transfer_total_seconds is not None else None
            while True:
                if guard:
                    guard()
                if reader:
                    reader.poll()
                try:
                    code = process.wait(timeout=0.2 if guard or telemetry else 1)
                    break
                except subprocess.TimeoutExpired:
                    if deadline is not None and time.monotonic() >= deadline:
                        raise
            if guard:
                guard()
            if reader:
                reader.poll()
        except subprocess.TimeoutExpired as exc:
            raise RsyncError(30, "rsync supervisor timed out.") from exc
        finally:
            process.stdin.close()
            try:
                process.wait(timeout=25)
            except subprocess.TimeoutExpired:
                # Do not start another writer: its worker.lock remains held.
                raise ConfigError("Transfer worker is still active; retry resume after it exits.")
            process.stdout.close()
    if code != 0:
        if code == 20:
            raise KeyboardInterrupt
        raise RsyncError(code, f"rsync returned {code}; batch is incomplete. See {log}.")
