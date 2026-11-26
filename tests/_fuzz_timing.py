"""Shared timing and linear-scaling helpers for fuzz regression suites."""
from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any


def machine_unit() -> float:
    """Seconds one fixed pure-Python loop takes on this machine.

    Provides a machine-relative yardstick so linear-time budgets stay
    meaningful across fast local workstations and slower shared CI runners.
    """
    best = float("inf")
    for _ in range(5):
        started = time.perf_counter()
        total = 0
        for index in range(200_000):
            total += index & 3
        best = min(best, time.perf_counter() - started)
    return best


def time_once(
    fn: Callable[[Any], Any],
    value: Any,
    *,
    ignore_errors: tuple[type[BaseException], ...] = (),
) -> float:
    """Measure one call to ``fn(value)`` with ``time.perf_counter()``."""
    started = time.perf_counter()
    try:
        fn(value)
    except ignore_errors:
        pass
    return time.perf_counter() - started


def assert_scales_linearly(
    fn: Callable[[Any], Any],
    small_input: Any,
    big_input: Any,
    *,
    label: str,
    ratio_limit: float = 9.0,
    ceiling_s: float | None = None,
    small_budget_s: float | None = None,
    floor_s: float = 1e-3,
    skip_ratio_below_s: float | None = None,
    small_len: int | None = None,
    big_len: int | None = None,
    ignore_errors: tuple[type[BaseException], ...] = (),
    context: str = "",
) -> tuple[float, float]:
    """Interleaved running-minima scaling check: linear ~4x, quadratic ~16x.

    Small and big timings are interleaved and the running minima kept over up
    to three rounds (3, 5, 8 repeats). Passing on the first round below
    ``ratio_limit`` resists scheduler jitter while keeping real quadratics
    above the limit in every round.
    """
    n_small = len(small_input) if small_len is None else small_len
    n_big = len(big_input) if big_len is None else big_len
    assert n_big >= 3 * n_small, label
    small = big = float("inf")
    ratio = 0.0
    for repeats in (3, 5, 8):
        for _ in range(repeats):
            small = min(small, time_once(fn, small_input, ignore_errors=ignore_errors))
            big = min(big, time_once(fn, big_input, ignore_errors=ignore_errors))
        ratio = big / max(small, floor_s)
        if (skip_ratio_below_s is not None and big < skip_ratio_below_s) or ratio < ratio_limit:
            break
    suffix = f" ({context})" if context else ""
    if ceiling_s is not None:
        assert big < ceiling_s, (
            f"{label}: {n_big} chars took {big:.3f}s "
            f"(ceiling {ceiling_s:.3f}s){suffix}"
        )
    if skip_ratio_below_s is None or big >= skip_ratio_below_s:
        assert ratio < ratio_limit, (
            f"{label}: x{n_big / n_small:.1f} input cost x{ratio:.1f} "
            f"({small * 1000:.1f} ms -> {big * 1000:.1f} ms), super-linear{suffix}"
        )
    if small_budget_s is not None:
        assert small < small_budget_s, (
            f"{label}: {n_small} chars took {small * 1000:.0f} ms "
            f"(budget {small_budget_s * 1000:.0f} ms){suffix}"
        )
    return small, big
