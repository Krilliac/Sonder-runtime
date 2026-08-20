"""Scalar compatibility-environment policy for the configuration boundary.

The typed configuration loader owns precedence and section composition.  This
module owns only the small, deterministic coercions used when importing the
historical ``SONDER_*`` environment variables.
"""
from __future__ import annotations

import os


def env_bool(value: str) -> bool:
    """Interpret the historical truthy environment spellings."""
    return value.strip().lower() in ("1", "true", "yes", "on")


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


__all__ = ["env_bool", "env_int", "env_float"]
