"""Standard-library-only helper sent in memory to a Linux SSH server (Python >=3.8)."""
import fcntl
import hashlib
import json
import os
import re
import select
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path


NETWORK = {"nfs", "nfs4", "cifs", "smb3", "smbfs", "sshfs", "9p"}
FIELDS = ("st_mode", "st_dev", "st_ino", "st_nlink", "st_uid", "st_gid", "st_size", "st_atime_ns", "st_mtime_ns", "st_ctime_ns")


class PolicyError(ValueError):
    pass


class Changed(ValueError):
    pass


def machine_id():
    value = Path("/etc/machine-id").read_text().strip()
    if not value:
        raise PolicyError("A non-empty /etc/machine-id is required for SSH endpoint identity.")
    return value


def unescape(value):
    for a, b in (("\\040", " "), ("\\011", "\t"), ("\\012", "\n"), ("\\134", "\\")):
        value = value.replace(a, b)
    return value


def mount(path):
    matches = []
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        before, after = line.split(" - ", 1)
        fields, extra = before.split(), after.split()
        point = unescape(fields[4])
        if str(path) == point or str(path).startswith(point.rstrip("/") + "/"):
            matches.append({"point": point, "root": unescape(fields[3]), "device": fields[2],
                            "options": fields[5].split(","), "fs": extra[0],
                            "source": unescape(extra[1]), "mount_id": fields[0]})
    if not matches:
        raise PolicyError("Cannot identify remote filesystem.")
    return max(matches, key=lambda value: len(value["point"]))


def no_links(path):
    for part in (path, *path.parents):
        try:
            if stat.S_ISLNK(part.lstat().st_mode):
                raise PolicyError("Remote symlink path is forbidden.")
        except FileNotFoundError:
            pass


def identity(root):
    no_links(root)
    info = root.stat()
    if not stat.S_ISDIR(info.st_mode):
        raise PolicyError("Remote root must be an existing directory.")
    filesystem = mount(root)
    return {"path": str(root), "type": "rsync_ssh", "device": info.st_dev, "inode": info.st_ino,
            "machine_id": machine_id(), "mount": filesystem, "read_only_mount": "ro" in filesystem["options"]}


def generation(info):
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def metadata(path):
    before = path.stat()
    attributes = {name: hashlib.sha256(os.getxattr(path, name, follow_symlinks=False)).hexdigest()
                  for name in sorted(os.listxattr(path, follow_symlinks=False))}
    if generation(path.stat()) != generation(before):
        raise Changed("Remote metadata changed while reading.")
    return {"mode": stat.S_IMODE(before.st_mode), "uid": before.st_uid, "gid": before.st_gid, "xattrs_sha256": attributes}


def validate_state(root, state):
    no_links(state)
    existing = state
    while not existing.exists():
        existing = existing.parent
    fs = mount(existing)
    if fs["fs"] in NETWORK or fs["fs"].startswith("fuse."):
        raise PolicyError("Remote lease directory must be on a local filesystem.")
    if root == state or root in state.parents or state in root.parents:
        raise PolicyError("Remote lease directory overlaps the endpoint.")
    root_fs = mount(root)
    root_physical = Path(root_fs["root"]) / root.relative_to(root_fs["point"])
    state_physical = Path(fs["root"]) / state.relative_to(fs["point"])
    if root.stat().st_dev == existing.stat().st_dev and (root_physical == state_physical or
            root_physical in state_physical.parents or state_physical in root_physical.parents):
        raise PolicyError("Remote lease directory overlaps through a bind mount.")
    return {"path": str(state), "machine_id": machine_id(), "device": existing.stat().st_dev, "mount": fs}


def read_header():
    data = bytearray()
    while not data.endswith(b"\n"):
        chunk = os.read(0, 1)
        if not chunk or len(data) >= 1048576:
            raise PolicyError("Missing/oversized SSH session header.")
        data.extend(chunk)
    header = json.loads(data)
    root, state = Path(header["root"]), Path(header["state"])
    for path in (root, state):
        if not path.is_absolute() or ".." in path.parts:
            raise PolicyError("Remote paths must be absolute without traversal.")
    lease_identity = validate_state(root, state)
    if header.get("lease_identity", lease_identity) != lease_identity:
        raise PolicyError("Remote lease filesystem changed; create a new plan.")
    return header, root, state


def idle_lease(state):
    no_links(state / "worker.lock")
    try:
        descriptor = os.open(state / "worker.lock", os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise PolicyError("Remote worker lease is unsafe.")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise PolicyError("Remote worker lease is active; wait before resuming.") from exc
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def rpc():
    header, root, state = read_header()
    expected = identity(root)
    if header.get("identity", expected) != expected:
        raise PolicyError("Remote endpoint changed after reconnect; create a new plan.")
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    if (os.fstat(descriptor).st_dev, os.fstat(descriptor).st_ino) != (expected["device"], expected["inode"]):
        raise PolicyError("Remote root changed while opening.")
    os.fchdir(descriptor)  # Metadata IO stays on the opened root even if its mount disappears.
    directories = {}
    next_id = 0
    for line in sys.stdin.buffer:
        lease = None
        try:
            request = json.loads(line)
            if identity(root) != expected:
                raise PolicyError("Remote endpoint or mount changed; create a new plan.")
            relative = Path(request.get("path", "."))
            if relative.is_absolute() or ".." in relative.parts:
                raise PolicyError("Remote path escapes root.")
            path = relative
            no_links(path)
            if mount(root / relative) != expected["mount"]:
                raise PolicyError("Remote entry crosses a nested mount.")
            op = request["op"]
            if op in ("utime", "write_test", "idle"):
                lease = idle_lease(state)
            result = None
            if op == "hello":
                version = subprocess.run(["rsync", "--version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15, check=True).stdout
                match = re.search(rb"version\s+(\d+)\.(\d+)\.(\d+)", version)
                if not match or tuple(map(int, match.groups())) < (3, 1, 3) or (
                        request.get("posix") and (b"no ACLs" in version or b"no xattrs" in version)):
                    raise PolicyError("Remote rsync >=3.1.3 with required ACL/xattr capabilities is needed.")
                result = {"identity": expected, "lease_identity": validate_state(root, state), "root_mtime_ns": root.stat().st_mtime_ns,
                          "readable": os.access(root, os.R_OK | os.X_OK), "writable": os.access(root, os.W_OK | os.X_OK),
                          "free_bytes": shutil.disk_usage(root).free, "protocol": 1}
            elif op == "identity":
                result = expected
            elif op == "mount":
                result = mount(root / relative)
            elif op == "stat":
                info = os.lstat(path)
                result = {name: getattr(info, name) for name in FIELDS}
            elif op == "open_dir":
                next_id += 1
                directories[next_id] = os.scandir(path)
                result = next_id
            elif op == "next_dir":
                iterator = directories[request["directory"]]
                result = []
                for _ in range(500):
                    try:
                        entry = next(iterator)
                    except StopIteration:
                        break
                    info = entry.stat(follow_symlinks=False)
                    result.append({"name": entry.name, "stat": {name: getattr(info, name) for name in FIELDS}})
            elif op == "close_dir":
                directories.pop(request["directory"]).close()
            elif op == "hash":
                before = path.stat()
                if not stat.S_ISREG(before.st_mode):
                    raise Changed("Remote hash requires a regular file.")
                original = generation(before)
                if request.get("expected") is not None and request["expected"] != original:
                    raise Changed("Remote source generation changed.")
                if request["algorithm"] not in ("sha256", "md5"):
                    raise PolicyError("Unsupported remote hash algorithm.")
                digest = hashlib.new(request["algorithm"])
                hashed, last_progress = 0, 0
                progress_interval = request.get("progress_interval", 1)
                if not 0 < progress_interval <= 1:
                    raise PolicyError("Invalid hash progress interval.")
                with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as file:
                    if generation(os.fstat(file.fileno())) != original:
                        raise Changed("Remote file changed while opening.")
                    while True:
                        chunk = file.read(1024 * 1024)
                        if not chunk:
                            break
                        digest.update(chunk)
                        hashed += len(chunk)
                        if time.monotonic() - last_progress >= progress_interval:
                            print(json.dumps({"progress_bytes": hashed}), flush=True)
                            last_progress = time.monotonic()
                    if generation(os.fstat(file.fileno())) != original or generation(path.stat()) != original:
                        raise Changed("Remote file changed while hashing.")
                no_links(path)
                result = [digest.hexdigest(), original]
            elif op == "metadata":
                result = metadata(path)
            elif op == "utime":
                if header["read_only"]:
                    raise PolicyError("Remote source writes are forbidden.")
                os.utime(path, ns=tuple(request["ns"]), follow_symlinks=False)
            elif op == "write_test":
                if header["read_only"]:
                    raise PolicyError("Remote source writes are forbidden.")
                import tempfile
                fd, name = tempfile.mkstemp(prefix=".migraflow-write-test-", dir=root)
                try:
                    with os.fdopen(fd, "wb") as file:
                        file.write(b"MigraFlow destination write test\n")
                        file.flush()
                        os.fsync(file.fileno())
                finally:
                    try:
                        os.unlink(name)
                    except OSError as exc:
                        raise PolicyError("Remote write-test cleanup failed; remaining path: " + name) from exc
            elif op not in ("check", "idle"):
                raise PolicyError("Unknown remote operation.")
            if identity(root) != expected:
                raise PolicyError("Remote endpoint changed during operation.")
            response = {"result": result}
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            response = {"error": type(exc).__name__, "message": str(exc), "errno": getattr(exc, "errno", None)}
        finally:
            if lease is not None:
                os.close(lease)
        print(json.dumps(response, ensure_ascii=True), flush=True)


def serve():
    header, root, state = read_header()
    if identity(root) != header["identity"]:
        raise PolicyError("Remote root identity differs from plan.")
    # The only rsync root argument is '.', sent with --secluded-args. User paths arrive in JSON.
    arguments = sys.argv[2:]
    if "--server" not in arguments or ("--sender" in arguments) != bool(header["read_only"]):
        raise PolicyError("SSH rsync direction differs from the configured role.")
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    no_links(state / "worker.lock")
    descriptor = os.open(state / "worker.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    if os.fstat(descriptor).st_nlink != 1:
        raise PolicyError("Remote worker lease must not be a hardlink.")
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    if (os.fstat(fd).st_dev, os.fstat(fd).st_ino) != (header["identity"]["device"], header["identity"]["inode"]):
        raise PolicyError("Remote root changed while opening.")
    os.fchdir(fd)
    process = None
    stopped = threading.Event()

    def stop():
        if process is not None and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                return
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()

    def interrupted(signum, frame):
        stop()
        raise SystemExit(20)

    signal.signal(signal.SIGHUP, interrupted)
    signal.signal(signal.SIGTERM, interrupted)
    process = subprocess.Popen(["rsync", *arguments], stdin=subprocess.PIPE, stdout=sys.stdout.buffer,
                               stderr=sys.stderr.buffer, start_new_session=True, pass_fds=(descriptor,))

    def forward():
        try:
            while not stopped.is_set():
                if not select.select([0], [], [], 0.2)[0]:
                    continue
                chunk = os.read(0, 65536)
                if not chunk:
                    stop()
                    break
                process.stdin.write(chunk)
                process.stdin.flush()
        except (BrokenPipeError, OSError):
            stop()

    thread = threading.Thread(target=forward, daemon=True)
    thread.start()
    try:
        while process.poll() is None:
            if identity(root) != header["identity"]:
                raise PolicyError("Remote mount/root changed during transfer.")
            try:
                process.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                pass
        return process.returncode
    finally:
        stopped.set()
        stop()
        thread.join(timeout=1)
        os.close(fd)
        os.close(descriptor)  # Release only after remote rsync is reaped.


def main():
    try:
        if sys.platform != "linux" or sys.version_info < (3, 8):
            raise PolicyError("Remote Linux and Python >=3.8 are required.")
        return serve() if sys.argv[1] == "serve" else rpc()
    except (OSError, ValueError) as exc:
        print("MigraFlow SSH helper: " + str(exc), file=sys.stderr)
        return 11


if __name__ == "__main__":
    raise SystemExit(main())
