"""Pure policy for normalizing master-orchestration timeouts."""

from __future__ import annotations

from typing import cast


def master_timeout(value: object, fallback: int, minimum: int, maximum: int) -> int:
    """Return an integer timeout bounded by the orchestration limits.

    Environment lookup belongs to the caller; this policy only normalizes
    the supplied value and falls back safely when it is absent or invalid.
    """
    try:
        normalized = int(cast(int, value))
    except (TypeError, ValueError):
        normalized = fallback
    return max(minimum, min(normalized, maximum))
