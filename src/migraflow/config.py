import math
from dataclasses import dataclass, field
from pathlib import Path

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


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    initial_delay_seconds: float = 1.0
    max_delay_seconds: float = 30.0


@dataclass(frozen=True)
class Config:
    source: Endpoint
    destination: Endpoint
    state_dir: Path
    hash_algorithm: str = "sha256"
    retry: RetryPolicy = field(default_factory=RetryPolicy)


def _keys(value, allowed, label):
    if not isinstance(value, dict) or set(value) - allowed:
        raise ConfigError(f"Invalid or unknown settings in {label}.")


def _path(value, base, label):
    if not isinstance(value, str) or not value.strip() or "\0" in value:
        raise ConfigError(f"{label} must be a non-empty path string.")
    path = Path(value).expanduser()
    return Path(path if path.is_absolute() else base / path).absolute()


def load_config(config_path=None, *, source=None, destination=None,
                source_type=None, destination_type=None, state_dir=None):
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
    _keys(data, {"source", "destination", "state_dir", "verification", "retry"}, "configuration")
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

    def endpoint(label, override_path, override_type):
        settings = data.get(label, {})
        _keys(settings, {"type", "path", "mount_source"}, label)
        kind = override_type or settings.get("type", "local")
        if kind not in ("local", "mounted"):
            raise ConfigError(f"{label}.type must be local or mounted; SSH is not implemented yet.")
        path = _path(override_path if override_path is not None else settings.get("path"),
                     Path.cwd() if override_path is not None else base, f"{label}.path")
        identity = settings.get("mount_source")
        if kind == "mounted" and (not isinstance(identity, str) or not identity.strip()):
            raise ConfigError(f"{label}.mount_source is required for mounted endpoints.")
        if kind == "local" and identity is not None:
            raise ConfigError(f"Remove {label}.mount_source when using local type.")
        return Endpoint(kind, path, identity)

    return Config(endpoint("source", source, source_type),
                  endpoint("destination", destination, destination_type),
                  _path(state_dir if state_dir is not None else data.get("state_dir", "state"),
                        Path.cwd() if state_dir is not None else base, "state_dir"), algorithm, policy)
