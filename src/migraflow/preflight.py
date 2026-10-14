import ctypes
import os
import re
import shutil
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path

from .config import ConfigError


NETWORK_FS = {"nfs", "nfs4", "cifs", "smb3", "9p", "drvfs", "fuse.sshfs"}


def _unescape(value):
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), value)


def mounts():
    records = []
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        left, right = line.split(" - ", 1)
        fields, extra = left.split(), right.split()
        record = {"point": _unescape(fields[4]), "root": _unescape(fields[3]),
                        "device": fields[2], "options": fields[5].split(","),
                        "fs": extra[0], "source": _unescape(extra[1])}
        if record["fs"] in NETWORK_FS or record["fs"].startswith("fuse."):
            record["mount_id"] = fields[0]
        records.append(record)
    return records


def _inside(path, root):
    return path == root or root in path.parents


def _no_links(path):
    for part in (path, *path.parents):
        if part.is_symlink() or part.is_junction():
            raise ConfigError(f"Symlink/junction path is not supported: {part}")


def filesystem(path):
    if sys.platform == "win32":
        drive_type = ctypes.windll.kernel32.GetDriveTypeW(str(path.anchor))
        if drive_type != 3:  # DRIVE_FIXED: no UNC, mapped network or removable drive DBs
            raise ConfigError(f"Only local fixed-drive paths are supported on Windows: {path}")
        return {"fs": "windows-local", "point": path.anchor,
                "source": path.anchor, "root": path.anchor, "options": []}
    if sys.platform != "linux":
        raise ConfigError("This planning release supports Windows local paths and Linux only.")
    candidates = [record for record in mounts() if _inside(path, Path(record["point"]))]
    if not candidates:
        raise ConfigError(f"Cannot identify filesystem: {path}")
    return max(candidates, key=lambda record: len(record["point"]))


def endpoint_identity(endpoint):
    _no_links(endpoint.path)
    path = endpoint.path.resolve(strict=True)
    if not path.is_dir():
        raise ConfigError(f"Endpoint must be an existing directory: {path}")
    mount = filesystem(path)
    remote = mount["fs"] in NETWORK_FS or mount["fs"].startswith("fuse.")
    if endpoint.type == "mounted":
        if sys.platform != "linux":
            raise ConfigError("mounted endpoints require Linux mount identity checks.")
        if not remote or mount["source"] != endpoint.mount_source:
            raise ConfigError(f"Expected NAS mount is absent or different: {path}")
    elif remote:
        raise ConfigError(f"Network/shared filesystem requires mounted type: {path}")
    info = path.stat()
    return {"path": str(path), "type": endpoint.type, "device": info.st_dev,
            "inode": info.st_ino, "mount": mount,
            "read_only_mount": "ro" in mount["options"]}


def state_path(path):
    _no_links(path)
    resolved = path.resolve()
    existing = resolved
    while not existing.exists():
        existing = existing.parent
    if not existing.is_dir():
        raise ConfigError("state_dir must be a directory, not a file.")
    mount = filesystem(existing)
    if mount["fs"] in NETWORK_FS or mount["fs"].startswith("fuse."):
        raise ConfigError("SQLite state_dir must be on a local filesystem.")
    return resolved


def _test_destination_write(path):
    try:
        fd, name = tempfile.mkstemp(prefix=".migraflow-write-test-", dir=path)
    except OSError as exc:
        raise ConfigError(f"Destination write test could not create a temporary file: {exc}") from exc
    try:
        try:
            with os.fdopen(fd, "wb") as file:
                file.write(b"MigraFlow destination write test\n")
                file.flush()
                os.fsync(file.fileno())
        except OSError as exc:
            raise ConfigError(f"Destination write test failed: {exc}") from exc
    finally:
        try:
            os.unlink(name)
        except OSError as exc:
            raise ConfigError(f"Destination write test cleanup failed; temporary file remains at {name}: {exc}") from exc


def preflight(config, *, test_destination_write=False):
    source = endpoint_identity(config.source)
    destination = endpoint_identity(config.destination)
    src, dst = Path(source["path"]), Path(destination["path"])
    if _inside(src, dst) or _inside(dst, src) or os.path.samefile(src, dst):
        raise ConfigError("Source and destination must be different, non-overlapping directories.")
    state = state_path(config.state_dir)
    for root in (src, dst):
        if _inside(state, root) or _inside(root, state):
            raise ConfigError("state_dir must not overlap source or destination.")
    # Bind mounts can expose nested directories through different path spellings.
    if sys.platform == "linux":
        physical = []
        state_mount = filesystem(state)
        state_existing = state
        while not state_existing.exists():
            state_existing = state_existing.parent
        state_identity = {"path": str(state), "mount": state_mount,
                          "device": state_existing.stat().st_dev}
        for identity in (source, destination, state_identity):
            mount = identity["mount"]
            physical.append(Path(mount["root"]) / Path(identity["path"]).relative_to(mount["point"]))
        same_share = (source["type"] == destination["type"] == "mounted"
                      and source["mount"]["fs"] == destination["mount"]["fs"]
                      and source["mount"]["source"] == destination["mount"]["source"])
        if (source["device"] == destination["device"] or same_share) and (
                _inside(physical[0], physical[1]) or _inside(physical[1], physical[0])):
            raise ConfigError("Source and destination overlap through bind mounts.")
        for index, identity in enumerate((source, destination)):
            if identity["device"] == state_identity["device"] and (
                    _inside(physical[2], physical[index]) or _inside(physical[index], physical[2])):
                raise ConfigError("state_dir overlaps an endpoint through bind mounts.")
    if not os.access(src, os.R_OK | os.X_OK) or not os.access(dst, os.R_OK | os.X_OK):
        raise ConfigError("Source and destination must be readable directories.")
    warnings = ["Metadata planning only; no content hash verification or transfer readiness is implied.",
                "Live files can change while scanning; use a stable snapshot for final migration."]
    if not source["read_only_mount"]:
        warnings.append("Source read-only protection is not proven; enforce it before any transfer.")
    if config.hash_algorithm == "md5":
        warnings.append("MD5 selected: collision resistance is weaker than SHA-256.")
    write_permission = not destination["read_only_mount"] and os.access(dst, os.W_OK | os.X_OK)
    if not write_permission:
        warnings.append("Destination write permission check failed; read-only planning is still available.")
    free_bytes = shutil.disk_usage(dst).free
    if test_destination_write:
        if endpoint_identity(config.source) != source or endpoint_identity(config.destination) != destination:
            raise ConfigError("Endpoint identity changed before destination write test.")
        if destination["read_only_mount"]:
            raise ConfigError("Destination write test refused: destination mount is read-only.")
        _test_destination_write(dst)
    return {"source": source, "destination": destination, "state_dir": str(state),
            "source_root_mtime_ns": src.stat().st_mtime_ns,
            "destination_free_bytes": free_bytes,
            "destination_write_permission": write_permission,
            "destination_write_test": "passed" if test_destination_write else "not_requested",
            "source_protection": "read-only mount" if source["read_only_mount"] else "unproven",
            "hash_algorithm": config.hash_algorithm, "transfer_ready": False, "warnings": warnings}


def config_data(config):
    data = asdict(config)
    data["source"]["path"] = str(config.source.path.resolve())
    data["destination"]["path"] = str(config.destination.path.resolve())
    data["state_dir"] = str(config.state_dir.resolve())
    return data
