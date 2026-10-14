import hashlib
import os
import stat

from .config import ConfigError
from .preflight import _no_links


class VerificationError(ValueError):
    pass


def generation(info):
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def file_generation(path):
    _no_links(path)
    info = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode):
        raise VerificationError(f"Expected a regular file: {path}")
    return generation(info)


def hash_file(path, algorithm, expected=None):
    _no_links(path)
    identity = file_generation(path)
    if expected is not None and identity != expected:
        raise VerificationError(f"Source generation changed: {path}")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(path, flags), "rb") as handle:
        before = os.fstat(handle.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise VerificationError(f"Expected a regular file: {path}")
        handle_identity = generation(before)
        # CPython 3.12 on Windows reports different ctime semantics for stat/fstat.
        # Compare inode/device/size/mtime across APIs, and ctime within each API.
        if handle_identity[:4] != identity[:4]:
            raise VerificationError(f"File changed while opening: {path}")
        # Fail if this Python build disables MD5; never silently use another algorithm.
        try:
            digest = hashlib.new(algorithm)
        except ValueError as exc:
            raise ConfigError(f"Hash algorithm unavailable in this Python build: {algorithm}") from exc
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
        if generation(os.fstat(handle.fileno())) != handle_identity or file_generation(path) != identity:
            raise VerificationError(f"File changed while hashing: {path}")
    return digest.hexdigest(), identity
