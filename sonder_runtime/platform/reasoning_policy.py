"""Platform policy for exposing model reasoning to callers."""

from __future__ import annotations

import os


_ENABLED_VALUES = frozenset({"1", "true", "yes", "on"})


def exposure_enabled(*, environ=None) -> bool:
    """Return whether the deployment asks models to expose their reasoning."""
    values = os.environ if environ is None else environ
    return str(values.get("SONDER_EXPOSE_REASONING", "")).strip().lower() in (
        _ENABLED_VALUES
    )


__all__ = ["exposure_enabled"]
