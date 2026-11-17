"""Pure policy helpers for the HTTP serving interface.

This module intentionally has no dependency on the server composition root.
The environment is read at call time so a long-running process and focused
tests observe the same live configuration semantics as the legacy helper.
"""
from __future__ import annotations

import logging
import math
import os
import threading
from contextlib import contextmanager

logger = logging.getLogger(__name__)


# ``None`` is "auto": the serve route sends no temperature, so the model
# family's preset applies (see sonder_runtime.domain.sampling_temperature).
SERVE_TEMPERATURE_DEFAULT = None
_AUTO_WORDS = frozenset({"auto", "default", "preset"})

_temperature_override = threading.local()


@contextmanager
def serve_temperature_override(value: float):
    """Temporarily force serve_temperature() for the current thread only."""
    previous = getattr(_temperature_override, "value", None)
    _temperature_override.value = float(value)
    try:
        yield float(value)
    finally:
        if previous is None:
            if hasattr(_temperature_override, "value"):
                delattr(_temperature_override, "value")
        else:
            _temperature_override.value = previous


def serve_temperature_override_value() -> float | None:
    """The value forced on this thread by serve_temperature_override, if any."""
    override = getattr(_temperature_override, "value", None)
    return None if override is None else float(override)


def serve_lane_temperature() -> float | None:
    """The operator's ``SONDER_SERVE_TEMPERATURE`` for the serve route.

    Unset, ``auto``, malformed and non-finite values mean ``None`` (send no
    temperature: the model preset applies); finite values are clamped to
    Ollama's inclusive ``0.0``-``2.0`` range, as before.
    """
    raw = os.environ.get("SONDER_SERVE_TEMPERATURE", "").strip()
    if not raw or raw.lower() in _AUTO_WORDS:
        logger.debug("serve_temperature: unset, using the model preset")
        return SERVE_TEMPERATURE_DEFAULT
    try:
        value = float(raw)
    except ValueError:
        logger.warning(f"SONDER_SERVE_TEMPERATURE has malformed value={raw!r}, using the model preset")
        return SERVE_TEMPERATURE_DEFAULT
    if not math.isfinite(value):
        logger.warning(f"SONDER_SERVE_TEMPERATURE has non-finite value={value}, using the model preset")
        return SERVE_TEMPERATURE_DEFAULT
    clamped = min(2.0, max(0.0, value))
    if clamped != value:
        logger.warning(f"SONDER_SERVE_TEMPERATURE value={value} clamped to {clamped} (valid range 0.0-2.0)")
    logger.debug(f"serve_temperature: resolved={clamped} (raw={raw!r})")
    return clamped


def serve_temperature() -> float | None:
    """The serve route's temperature before any session setting.

    A per-thread override wins; otherwise ``SONDER_SERVE_TEMPERATURE``;
    ``None`` means none is sent and the model preset applies.
    """
    override = serve_temperature_override_value()
    if override is not None:
        return override
    return serve_lane_temperature()
