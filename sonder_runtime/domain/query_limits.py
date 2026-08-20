"""Pure policy for normalizing bounded integer query limits."""

from __future__ import annotations


def safe_limit(limit, default=10, max_value=100):
    """Coerce a caller-provided limit into the inclusive range ``1..max_value``.

    Invalid values retain the caller's default before the result is clamped.
    This is deliberately side-effect free so each tool can apply its own
    default and ceiling without owning a duplicate parsing policy.
    """
    try:
        value = int(limit)
    except (TypeError, ValueError):
        value = default
    return max(1, min(value, max_value))


__all__ = ["safe_limit"]
