"""Pure policy for the bounded lesson-distillation call budget."""

from __future__ import annotations

from collections.abc import Callable


def distillation_timeout_seconds(
    env_int_option: Callable[[str, int], int | None],
    live_ceiling: int,
) -> int:
    """Return the independently configurable, bounded distillation budget.

    Environment parsing remains supplied by the platform boundary.  The
    domain policy owns the stable default and lower/upper bounds while the
    caller supplies the live server ceiling.
    """
    value = env_int_option("SONDER_DISTILLATION_TIMEOUT", 20)
    if value is None:
        value = 20
    return max(1, min(int(value), int(live_ceiling)))
