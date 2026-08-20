"""Pure status normalization policy for doctor checks."""

from __future__ import annotations

from typing import Any


STATUS_OK = "ok"
STATUS_WARN = "warn"
STATUS_FAIL = "fail"
STATUS_SKIPPED = "skipped"

_VALID_STATUSES = frozenset(
    {STATUS_OK, STATUS_WARN, STATUS_FAIL, STATUS_SKIPPED}
)


def coerce_status(value: Any) -> str:
    """Map an arbitrary check verdict onto the fixed status vocabulary."""
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in _VALID_STATUSES:
            return lowered
        synonyms = {
            "pass": STATUS_OK,
            "passed": STATUS_OK,
            "healthy": STATUS_OK,
            "good": STATUS_OK,
            "warning": STATUS_WARN,
            "degraded": STATUS_WARN,
            "attention": STATUS_WARN,
            "watch": STATUS_WARN,
            "error": STATUS_FAIL,
            "failed": STATUS_FAIL,
            "critical": STATUS_FAIL,
            "skip": STATUS_SKIPPED,
        }
        return synonyms.get(lowered, STATUS_FAIL)
    if isinstance(value, bool):
        return STATUS_OK if value else STATUS_FAIL
    return STATUS_FAIL
