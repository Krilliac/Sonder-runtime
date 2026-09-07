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
_NON_PHYSICAL_LINE_SEPARATORS = frozenset("\v\f\x1c\x1d\x1e\x85\u2028\u2029")
_SENSITIVE_ENV_FILE_KEY_POLICIES = {
    _MOBILITY_PEER_KEY: (
        "artifact_mobility_peer_key",
        _MOBILITY_PEER_KEY_ERROR,
    ),
}


class EnvironmentFileError(ValueError):
    """Malformed compatibility environment-file input.

    ``field_code`` is deliberately metadata instead of a copy of the rejected
    line.  The configuration boundary can preserve legacy diagnostics for
    ordinary compatibility keys while keeping mobility credentials out of
    exceptions, logs, and serialization.
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


def _ambiguous_sensitive_key_policy(raw_text: str) -> tuple[str, str] | None:
    """Find a sensitive key crossed by a ``str.splitlines``-only separator.

    The normal parser remains line-oriented for compatibility, but its error
    rendering must not depend on separators that ``splitlines`` silently turns
    into records. Inspect each physical CR/LF record before that normalization;
    collapsing those non-physical separators also catches a sensitive key that
    was split in the middle. New sensitive keys add a policy entry rather than
    another parser-specific adjacency rule.
    """
    physical_lines = raw_text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    for physical_line in physical_lines:
        if not any(
            character in _NON_PHYSICAL_LINE_SEPARATORS
            for character in physical_line
        ):
            continue
        collapsed = "".join(
            character
            for character in physical_line
            if character not in _NON_PHYSICAL_LINE_SEPARATORS
        )
        if policy := _sensitive_key_policy_in(collapsed):
            return policy
    return None


def _raise_sensitive_key_error(policy: tuple[str, str]) -> None:
    field_code, message = policy
    raise EnvironmentFileError(message, field_code=field_code)


def parse_env_file(path: Path) -> dict[str, str]:
    """Parse a ``KEY=VALUE`` environment file without owning config types."""
    values: dict[str, str] = {}
    raw_text = path.read_text(encoding="utf-8")
    if policy := _ambiguous_sensitive_key_policy(raw_text):
        _raise_sensitive_key_error(policy)
    sensitive_key_continuation_policy: tuple[str, str] | None = None
    for lineno, raw in enumerate(raw_text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            # Comments and blank lines cannot prove a following rejected record
            # was unrelated to a sensitive secret split by an input separator.
            continue
        if "=" not in line:
            policy = _sensitive_key_policy_in(line) or sensitive_key_continuation_policy
            if policy:
                _raise_sensitive_key_error(policy)
            raise EnvironmentFileError(
                f"{path}:{lineno}: expected KEY=VALUE, got {line[:32]!r}"
            )
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
        sensitive_key_continuation_policy = policy
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
