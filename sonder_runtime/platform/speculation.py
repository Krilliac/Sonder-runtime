"""Packaged configuration boundary for speculative execution.

The root ``sonder_speculation`` module remains a compatibility surface for
the predictor and engine.  Environment-backed tuning and predictor-state
location live here so packaged runtime code has one configuration boundary.
"""
from __future__ import annotations

import os
from pathlib import Path

from sonder_runtime.platform.paths import state_path


DEFAULT_MIN_SAVING_MS = 40.0
DEFAULT_SLOTS = 1
MAX_SLOTS = 4


def min_saving_seconds() -> float:
    """Return the non-negative expected-saving floor in seconds."""
    raw = os.environ.get("SONDER_SPECULATION_MIN_SAVING_MS", "").strip()
    try:
        milliseconds = float(raw) if raw else DEFAULT_MIN_SAVING_MS
    except ValueError:
        milliseconds = DEFAULT_MIN_SAVING_MS
    return max(0.0, milliseconds) / 1000.0


def speculation_slots() -> int:
    """Return the bounded number of concurrent speculation slots."""
    raw = os.environ.get("SONDER_SPECULATION_SLOTS", "").strip()
    try:
        slots = int(raw) if raw else DEFAULT_SLOTS
    except ValueError:
        slots = DEFAULT_SLOTS
    return max(1, min(MAX_SLOTS, slots))


def predictor_path() -> Path:
    """Return the configured predictor file under the packaged state path."""
    override = os.environ.get("SONDER_BRANCH_PREDICTOR", "").strip()
    if override:
        return Path(override).expanduser()
    return Path(state_path("branch_predictor.json"))


__all__ = [
    "DEFAULT_MIN_SAVING_MS",
    "DEFAULT_SLOTS",
    "MAX_SLOTS",
    "min_saving_seconds",
    "predictor_path",
    "speculation_slots",
]
