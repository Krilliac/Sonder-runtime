"""Scalar compatibility-environment policy for the configuration boundary.

The typed configuration loader owns precedence and section composition.  This
module owns only the small, deterministic coercions used when importing the
historical ``SONDER_*`` environment variables.
"""
from __future__ import annotations

import os
from pathlib import Path


class EnvironmentFileError(ValueError):
    """Malformed compatibility environment-file input."""


_NONCANONICAL_LINE_SEPARATORS = frozenset(("\u0085", "\u2028", "\u2029"))


def _unsafe_input_offset(value: str) -> int | None:
    """Return the first non-CRLF control or noncanonical line separator."""
    for index, character in enumerate(value):
        if character == "\r":
            if index + 1 < len(value) and value[index + 1] == "\n":
                continue
            return index
        if character == "\n":
            continue
        if (
            ord(character) < 32
            or ord(character) == 127
            or character in _NONCANONICAL_LINE_SEPARATORS
        ):
            return index
    return None


def parse_env_file(path: Path) -> dict[str, str]:
    """Parse a ``KEY=VALUE`` file without reflecting malformed secret input."""
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            content = handle.read()
    except UnicodeDecodeError:
        raise EnvironmentFileError(
            f"{path}: malformed secrets environment input"
        ) from None

    unsafe_offset = _unsafe_input_offset(content)
    if unsafe_offset is not None:
        line_number = content.count("\n", 0, unsafe_offset) + 1
        raise EnvironmentFileError(
            f"{path}:{line_number}: malformed secrets environment input"
        )

    values: dict[str, str] = {}
    for lineno, raw in enumerate(content.split("\n"), start=1):
        line = raw[:-1] if raw.endswith("\r") else raw
        line = line.strip(" ")
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise EnvironmentFileError(
                f"{path}:{lineno}: expected KEY=VALUE"
            )
        key, _, value = line.partition("=")
        key = key.strip(" ")
        value = value.strip(" ")
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
