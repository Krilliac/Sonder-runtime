"""Bounded local model retry configuration and backoff policy."""

from __future__ import annotations

import os


MAX_LOCAL_MODEL_RETRIES = 2


def local_model_retries() -> int:
    """Return the operator-configured number of local retry attempts."""
    raw = os.environ.get("SONDER_LOCAL_RETRIES", "1").strip()
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = 1
    return max(0, min(value, MAX_LOCAL_MODEL_RETRIES))


def retry_delay(attempt: int) -> float:
    """Return bounded exponential backoff for a one-based retry attempt."""
    raw = os.environ.get("SONDER_LOCAL_RETRY_DELAY_MS", "150").strip()
    try:
        base_ms = float(raw)
    except (TypeError, ValueError):
        base_ms = 150.0
    base_ms = max(0.0, min(base_ms, 1000.0))
    return min(1.0, (base_ms / 1000.0) * (2 ** max(0, attempt - 1)))


__all__ = ["MAX_LOCAL_MODEL_RETRIES", "local_model_retries", "retry_delay"]
