"""Pure policy for normalizing master-orchestration timeouts."""

from __future__ import annotations


def master_timeout(value, fallback: int, minimum: int, maximum: int) -> int:
    """Return an integer timeout bounded by the orchestration limits.

    Environment lookup belongs to the caller; this policy only normalizes
    the supplied value and falls back safely when it is absent or invalid.
    """
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        normalized = fallback
    return max(minimum, min(normalized, maximum))
