import math
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import yaml


class ConfigError(ValueError):
    pass


class StrictLoader(yaml.SafeLoader):
    """Safe YAML, with duplicate keys rejected instead of silently overwritten."""


def _mapping(loader, node):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)
        if not isinstance(key, str) or key in result:
            raise ConfigError("YAML keys must be unique strings.")
        result[key] = loader.construct_object(value_node)
    return result


StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


@dataclass(frozen=True)
class Endpoint:
    type: str
    path: Path
    mount_source: str | None = None
    host: str | None = None
    remote_state_dir: PurePosixPath | None = None


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    initial_delay_seconds: float = 1.0
    max_delay_seconds: float = 30.0


@dataclass(frozen=True)
class TimeoutPolicy:
    transfer_idle_seconds: int = 1800
    transfer_total_seconds: float | None = None
    hash_idle_seconds: float = 1800
    hash_total_seconds: float | None = None


@dataclass(frozen=True)
class Config:
    source: Endpoint
    destination: Endpoint
    state_dir: Path
    hash_algorithm: str = "sha256"
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    metadata_policy: str = "basic"
    timeouts: TimeoutPolicy = field(default_factory=TimeoutPolicy)


def _keys(value, allowed, label):
    if not isinstance(value, dict) or set(value) - allowed:
        raise ConfigError(f"Invalid or unknown settings in {label}.")


def _path(value, base, label):
    if not isinstance(value, str) or not value.strip() or "\0" in value:
        raise ConfigError(f"{label} must be a non-empty path string.")
    path = Path(value).expanduser()
    return Path(path if path.is_absolute() else base / path).absolute()


def load_config(config_path=None, *, source=None, destination=None,
                source_type=None, destination_type=None, state_dir=None, metadata_policy=None,
                source_host=None, destination_host=None, source_remote_state_dir=None, destination_remote_state_dir=None):
    explicit = config_path is not None
    file = Path(config_path or "migration.local.yml")
    data = {}
    base = Path.cwd()
    if explicit or file.exists():
        try:
            data = yaml.load(file.read_text(encoding="utf-8-sig"), Loader=StrictLoader)
        except (OSError, UnicodeError, yaml.YAMLError) as exc:
            raise ConfigError(f"Cannot read YAML configuration: {exc}") from exc
        if data is None:
            data = {}
        base = file.absolute().parent
    _keys(data, {"source", "destination", "state_dir", "verification", "retry", "metadata", "timeouts"}, "configuration")
    timeout_data = data.get("timeouts", {})
    _keys(timeout_data, set(TimeoutPolicy.__dataclass_fields__), "timeouts")
    timeouts = TimeoutPolicy(**timeout_data)
    for key in TimeoutPolicy.__dataclass_fields__:
        value = getattr(timeouts, key)
        if value is None and key.endswith("total_seconds"):
            continue
        if type(value) not in (int, float) or not 0 < value <= 2147483647 or not math.isfinite(value):
            raise ConfigError(f"timeouts.{key} must be finite and >0 up to 2147483647 seconds (total limits also accept null).")
    if type(timeouts.transfer_idle_seconds) is not int:
        raise ConfigError("timeouts.transfer_idle_seconds must be a positive integer.")
    metadata = data.get("metadata", {})
    _keys(metadata, {"policy"}, "metadata")
    metadata_policy = metadata_policy or metadata.get("policy", "basic")
    if metadata_policy not in ("basic", "posix"):
        raise ConfigError("metadata.policy must be basic or posix.")
    retry = data.get("retry", {})
    _keys(retry, {"max_attempts", "initial_delay_seconds", "max_delay_seconds"}, "retry")
    policy = RetryPolicy(**retry)
    if type(policy.max_attempts) is not int or not 1 <= policy.max_attempts <= 10:
        raise ConfigError("retry.max_attempts must be an integer from 1 to 10.")
    for delay in (policy.initial_delay_seconds, policy.max_delay_seconds):
        if type(delay) not in (int, float) or not math.isfinite(delay) or not 0 <= delay <= 30:
            raise ConfigError("Retry delays must be finite numbers from 0 to 30 seconds.")
    if policy.initial_delay_seconds > policy.max_delay_seconds:
        raise ConfigError("retry.initial_delay_seconds must not exceed max_delay_seconds.")
    verification = data.get("verification", {})
    _keys(verification, {"algorithm"}, "verification")
    algorithm = verification.get("algorithm", "sha256")
    if algorithm not in ("sha256", "md5"):
        raise ConfigError("verification.algorithm must be sha256 or md5.")

    def endpoint(label, override_path, override_type, override_host, override_remote_state):
        settings = data.get(label, {})
        _keys(settings, {"type", "path", "mount_source", "host", "remote_state_dir"}, label)
        kind = override_type or settings.get("type", "local")
        if kind not in ("local", "mounted", "rsync_ssh"):
            raise ConfigError(f"{label}.type must be local, mounted or rsync_ssh.")
        if kind == "rsync_ssh":
            host = override_host if override_host is not None else settings.get("host")
            if not isinstance(host, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,252}", host):
                raise ConfigError(f"{label}.host must be a plain OpenSSH host alias.")
            paths = []
            for key, value in (("path", override_path if override_path is not None else settings.get("path")),
                               ("remote_state_dir", override_remote_state if override_remote_state is not None else settings.get("remote_state_dir"))):
                if not isinstance(value, str) or not value.startswith("/") or "\0" in value or ".." in value.split("/"):
                    raise ConfigError(f"{label}.{key} must be an absolute POSIX path without '..'.")
                paths.append(PurePosixPath(value))
            if settings.get("mount_source") is not None:
                raise ConfigError("rsync_ssh does not accept mount_source.")
            return Endpoint(kind, paths[0], host=host, remote_state_dir=paths[1])
        if any(value is not None for value in (settings.get("host"), settings.get("remote_state_dir"), override_host, override_remote_state)):
            raise ConfigError("SSH settings require rsync_ssh type.")
        path = _path(override_path if override_path is not None else settings.get("path"),
                     Path.cwd() if override_path is not None else base, f"{label}.path")
        identity = settings.get("mount_source")
        if kind == "mounted" and (not isinstance(identity, str) or not identity.strip()):
            raise ConfigError(f"{label}.mount_source is required for mounted endpoints.")
        if kind == "local" and identity is not None:
            raise ConfigError(f"Remove {label}.mount_source when using local type.")
        return Endpoint(kind, path, identity)

    return Config(endpoint("source", source, source_type, source_host, source_remote_state_dir),
                  endpoint("destination", destination, destination_type, destination_host, destination_remote_state_dir),
                  _path(state_dir if state_dir is not None else data.get("state_dir", "state"),
                        Path.cwd() if state_dir is not None else base, "state_dir"), algorithm, policy, metadata_policy, timeouts)
