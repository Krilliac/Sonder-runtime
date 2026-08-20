"""Pure policies for normalizing provider-reported model usage."""

from __future__ import annotations


def usage_count(value):
    """Return a non-negative integer usage count, or ``None`` if invalid."""
    if value is None:
        return None
    try:
        value = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return value if value >= 0 else None
