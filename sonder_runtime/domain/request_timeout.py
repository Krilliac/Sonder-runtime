"""Pure policy for bounding a model request timeout."""

from __future__ import annotations


def bound_request_timeout(value, ceiling: int) -> int:
    """Return a positive timeout no greater than the configured ceiling.

    The caller owns the runtime default/ceiling lookup. Keeping that lookup
    outside this policy makes the normalization deterministic and reusable by
    adapters without importing the root server module.
    """
    try:
        value = ceiling if value is None else int(value)
    except (TypeError, ValueError):
        value = ceiling
    return max(1, min(value, ceiling))
