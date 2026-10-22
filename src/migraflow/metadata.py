"""POSIX metadata snapshots; attribute values are hashed rather than stored."""
import hashlib
import json
import os
import stat

from .config import ConfigError
from .preflight import _no_links
from .verifier import VerificationError, generation
from .ssh import RemotePath


def capture(path):
    if isinstance(path, RemotePath):
        return path.call("metadata")
    _no_links(path)
    before = path.stat(follow_symlinks=False)
    try:
        attributes = {name: hashlib.sha256(os.getxattr(path, name, follow_symlinks=False)).hexdigest()
                      for name in sorted(os.listxattr(path, follow_symlinks=False))}
    except OSError as exc:
        raise ConfigError(f"POSIX attribute reading is unsupported or denied: {path}: {exc}") from exc
    _no_links(path)
    if generation(path.stat(follow_symlinks=False)) != generation(before):
        raise VerificationError(f"Metadata changed while reading: {path}")
    return {"mode": stat.S_IMODE(before.st_mode), "uid": before.st_uid, "gid": before.st_gid,
            "xattrs_sha256": attributes}


def snapshot(path):
    return json.dumps(capture(path), sort_keys=True, ensure_ascii=True)


def validate(path, expected):
    if expected is None or capture(path) != json.loads(expected):
        raise VerificationError(f"POSIX mode/owner/group/ACL/xattrs differ from plan: {path}")
