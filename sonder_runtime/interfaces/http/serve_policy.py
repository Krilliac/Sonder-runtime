"""Pure policy helpers for the HTTP serving interface.

This module intentionally has no dependency on the server composition root.
The environment is read at call time so a long-running process and focused
tests observe the same live configuration semantics as the legacy helper.
"""
from __future__ import annotations

import math
import os


SERVE_TEMPERATURE_DEFAULT = 0.2


def serve_temperature() -> float:
    """Return the bounded sampling temperature for the serve chat route.

    Missing, malformed, non-finite, and out-of-range values preserve the
    legacy contract: malformed values use the default, while finite values
    are clamped to Ollama's inclusive ``0.0``–``2.0`` range.
    """
    raw = os.environ.get("SONDER_SERVE_TEMPERATURE", "").strip()
    if not raw:
        return SERVE_TEMPERATURE_DEFAULT
    try:
        value = float(raw)
    except ValueError:
        return SERVE_TEMPERATURE_DEFAULT
    if not math.isfinite(value):
        return SERVE_TEMPERATURE_DEFAULT
    return min(2.0, max(0.0, value))
