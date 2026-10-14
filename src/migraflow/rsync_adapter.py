import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

from .config import ConfigError


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


def transfer_batch(prefix, source, destination, paths, manifest, log, *, overwrite=False, partial_dir=None):
    # Windows relative paths use backslashes; POSIX filenames may contain them.
    manifest.write_bytes(b"".join((path.replace(b"\\", b"/") if os.name == "nt" else path) + b"\0"
                                  for path in paths))
    policy = [] if overwrite else ["--ignore-existing"]
    if partial_dir:
        policy += ["--partial-dir=" + partial_dir, "--no-whole-file"]
    arguments = prefix + policy + ["--dirs", "--times", "--modify-window=-1", "--ignore-times", "--no-recursive", "--no-links",
                          "--from0", "--files-from=-", "--", rsync_path(source) + "/", rsync_path(destination) + "/"]
    worker = Path(__file__).with_name("worker.py")
    helper = [sys.executable, str(worker), "--lock", str(manifest.parent.parent / "worker.lock"),
              "--manifest", str(manifest), "--timeout", "1820" if os.name == "nt" else "1800"]
    if os.name == "nt":
        arguments = [prefix[0], "--exec", "python3", rsync_path(worker), "--manifest", rsync_path(manifest),
                     "--timeout", "1800", "--", *arguments[2:]]
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
            process.stdin.write(b"1")
            process.stdin.flush()
            code = process.wait(timeout=1840)
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
