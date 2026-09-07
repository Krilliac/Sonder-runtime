"""Scalar compatibility-environment policy for the configuration boundary.

The typed configuration loader owns precedence and section composition.  This
module owns only the small, deterministic coercions used when importing the
historical ``SONDER_*`` environment variables.
"""
from __future__ import annotations

import os
from pathlib import Path


_MOBILITY_PEER_KEY = "SONDER_ARTIFACT_MOBILITY_PEER_KEY"
_MOBILITY_PEER_KEY_ERROR = "[artifact_mobility].peer_key malformed secrets input"
_SENSITIVE_ENV_FILE_KEY_POLICIES = {
    _MOBILITY_PEER_KEY: (
        "artifact_mobility_peer_key",
        _MOBILITY_PEER_KEY_ERROR,
    ),
}


class EnvironmentFileError(ValueError):
    """Malformed compatibility environment-file input.

    ``field_code`` is deliberately metadata instead of a copy of the rejected
    line. The configuration boundary can retain a stable error category and
    line location without ever reflecting malformed file content into
    exceptions, logs, or serialization.
    """

    def __init__(self, message: str, *, field_code: str = "") -> None:
        super().__init__(message)
        self.field_code = field_code


def _sensitive_key_policy_in(value: str) -> tuple[str, str] | None:
    """Return the non-disclosing policy for a sensitive key-shaped input."""
    for key, policy in _SENSITIVE_ENV_FILE_KEY_POLICIES.items():
        if key in value:
            return policy
    return None


def _raise_sensitive_key_error(policy: tuple[str, str]) -> None:
    field_code, message = policy
    raise EnvironmentFileError(message, field_code=field_code)


def parse_env_file(path: Path) -> dict[str, str]:
    """Parse a ``KEY=VALUE`` environment file without owning config types."""
    values: dict[str, str] = {}
    for lineno, raw in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            if policy := _sensitive_key_policy_in(line):
                _raise_sensitive_key_error(policy)
            # File content can be a secret even when no known key precedes it.
            raise EnvironmentFileError(f"{path}:{lineno}: expected KEY=VALUE")
        key, _, value = line.partition("=")
        key = key.strip()
        policy = _SENSITIVE_ENV_FILE_KEY_POLICIES.get(key)
        if policy and any(
            ord(character) < 32 or ord(character) == 127
            for character in raw.partition("=")[2]
        ):
            _raise_sensitive_key_error(policy)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key] = value
    return values


def env_bool(value: str) -> bool:
    """Interpret the historical truthy environment spellings."""
    return value.strip().lower() in ("1", "true", "yes", "on")


def env_bool_from_env(
    name: str,
    default: bool = False,
    *,
    environ: dict[str, str] | None = None,
) -> bool:
    """Read a named compatibility boolean while preserving its default."""
    source = environ if environ is not None else os.environ
    raw = source.get(name, "").strip()
    return default if not raw else env_bool(raw)


def env_int(name: str, env: dict[str, str], current: int, errors: list[str]) -> int:
    """Read one compatibility integer without bypassing typed validation."""
    raw = env.get(name, "").strip()
    if not raw:
        return current
    try:
        return int(raw)
    except ValueError:
        errors.append(f"{name} is not an integer")
        return current


def env_float(
    name: str,
    default: float | None = None,
    *,
    environ: dict[str, str] | None = None,
) -> float | None:
    """Read one optional non-negative compatibility float from an environment mapping."""
    source = environ if environ is not None else os.environ
    raw = source.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return default


__all__ = [
    "EnvironmentFileError",
    "env_bool",
    "env_bool_from_env",
    "env_int",
    "env_float",
    "parse_env_file",
]
