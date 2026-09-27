"""Frame-time statistics and spike detection.

Percentiles are nearest-rank (the value at rank ``ceil(p/100 * n)`` of the
sorted series), so p95/p99 are always observed frame times, never
interpolations.

Spikes are points above ``median + k * scale`` where

    scale = max(1.4826 * MAD, 0.05 * median, 0.5 ms)

The floor matters: on a flat 16.6 ms series the MAD is about 0, and without it
an 18 ms frame would be flagged. With it, a 16.6 ms series has a threshold
near 19.1 ms (k=3), so 40 ms spikes are flagged and 18 ms frames are not.
"""
from __future__ import annotations

import math
from typing import Sequence

from sonder_runtime.domain.profiling.model import MAX_SPIKES, FrameStats, Spike

NS_PER_MS = 1_000_000
SCALE_FLOOR_NS = 500_000  # 0.5 ms
MAD_TO_SIGMA = 1.4826
MEDIAN_FLOOR_FRACTION = 0.05


def _finite_ns(values: Sequence[float]) -> list[float]:
    out = []
    for value in values:
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            continue
        if math.isfinite(number) and number >= 0:
            out.append(number)
    return out


def nearest_rank(sorted_values: Sequence[float], pct: float) -> float:
    """Nearest-rank percentile of an already sorted, non-empty series."""
    count = len(sorted_values)
    rank = max(1, min(count, math.ceil(pct / 100.0 * count)))
    return sorted_values[rank - 1]


def _median(sorted_values: Sequence[float]) -> float:
    count = len(sorted_values)
    middle = count // 2
    if count % 2:
        return float(sorted_values[middle])
    return (sorted_values[middle - 1] + sorted_values[middle]) / 2.0


def frame_stats(durations_ns: Sequence[float], budget_ms: float | None = None) -> FrameStats | None:
    """Frame-time summary in milliseconds, or None for an empty series."""
    values = sorted(_finite_ns(durations_ns))
    if not values:
        return None
    budget = None
    over = 0
    if budget_ms is not None:
        try:
            budget = float(budget_ms)
        except (TypeError, ValueError, OverflowError):
            budget = None
        if budget is not None and (not math.isfinite(budget) or budget <= 0):
            budget = None
    if budget is not None:
        limit = budget * NS_PER_MS
        over = sum(1 for value in values if value > limit)
    return FrameStats(
        count=len(values),
        p50_ms=nearest_rank(values, 50) / NS_PER_MS,
        p95_ms=nearest_rank(values, 95) / NS_PER_MS,
        p99_ms=nearest_rank(values, 99) / NS_PER_MS,
        max_ms=values[-1] / NS_PER_MS,
        budget_ms=budget,
        over_budget=over,
    )


def spike_threshold(series_ns: Sequence[float], k: float = 3.0) -> tuple[float, float]:
    """(median, threshold) for a series in nanoseconds."""
    values = sorted(_finite_ns(series_ns))
    if not values:
        return 0.0, math.inf
    median = _median(values)
    mad = _median(sorted(abs(value - median) for value in values))
    scale = max(MAD_TO_SIGMA * mad, MEDIAN_FLOOR_FRACTION * median, SCALE_FLOOR_NS)
    return median, median + float(k) * scale


def detect_spikes(
    series_ns: Sequence[float],
    *,
    k: float = 3.0,
    max_spikes: int = MAX_SPIKES,
    kind: str = "frame",
    label: str = "frame",
    starts_ns: Sequence[int | None] | None = None,
    labels: Sequence[str] | None = None,
    thread: str | None = None,
) -> tuple[Spike, ...]:
    """Points above ``median + k * max(1.4826*MAD, 0.05*median, 0.5 ms)``.

    Returns at most ``max_spikes`` spikes, largest ratio first (ties by index).
    ``label`` is suffixed with ``#index`` when no per-point ``labels`` are given.
    """
    points = []
    for index, value in enumerate(series_ns):
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            continue
        if math.isfinite(number) and number >= 0:
            points.append((index, number))
    if len(points) < 3:
        return ()
    median, threshold = spike_threshold([value for _, value in points], k)
    if median <= 0:
        return ()
    flagged = [(index, value) for index, value in points if value > threshold]
    flagged.sort(key=lambda item: (-item[1], item[0]))
    spikes = []
    for index, value in flagged[: max(0, min(int(max_spikes), MAX_SPIKES))]:
        start = None
        if starts_ns is not None and index < len(starts_ns):
            start = starts_ns[index]
        name = (labels[index] if labels is not None and index < len(labels)
                else "%s #%d" % (label, index))
        spikes.append(Spike(kind=kind, label=name, start_ns=start, duration_ns=int(value),
                            ratio_to_median=value / median, thread=thread))
    return tuple(spikes)
