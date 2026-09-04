"""Pure policy helpers for the HTTP serving interface.

This module intentionally has no dependency on the server composition root.
The environment is read at call time so a long-running process and focused
tests observe the same live configuration semantics as the legacy helper.
"""
from __future__ import annotations

import logging
import math
import os

logger = logging.getLogger(__name__)


SERVE_TEMPERATURE_DEFAULT = 0.2


def serve_temperature() -> float:
    """Return the bounded sampling temperature for the serve chat route.

    Missing, malformed, non-finite, and out-of-range values preserve the
    legacy contract: malformed values use the default, while finite values
    are clamped to Ollama's inclusive ``0.0``–``2.0`` range.
    """
    raw = os.environ.get("SONDER_SERVE_TEMPERATURE", "").strip()
    if not raw:
        logger.debug(f"serve_temperature: using default={SERVE_TEMPERATURE_DEFAULT}")
        return SERVE_TEMPERATURE_DEFAULT
    try:
        value = float(raw)
    except ValueError:
        logger.warning(f"SONDER_SERVE_TEMPERATURE has malformed value={raw!r}, falling back to default={SERVE_TEMPERATURE_DEFAULT}")
        logger.debug(f"serve_temperature: malformed env value={raw!r}, using default")
        return SERVE_TEMPERATURE_DEFAULT
    if not math.isfinite(value):
        logger.warning(f"SONDER_SERVE_TEMPERATURE has non-finite value={value}, falling back to default={SERVE_TEMPERATURE_DEFAULT}")
        logger.debug(f"serve_temperature: non-finite value={value}, using default")
        return SERVE_TEMPERATURE_DEFAULT
    clamped = min(2.0, max(0.0, value))
    if clamped != value:
        logger.warning(f"SONDER_SERVE_TEMPERATURE value={value} clamped to {clamped} (valid range 0.0-2.0)")
    logger.debug(f"serve_temperature: resolved={clamped} (raw={raw!r})")
    return clamped
