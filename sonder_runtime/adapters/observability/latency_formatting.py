"""Pure formatting policies for bounded observability latency summaries."""
from __future__ import annotations

import math


def percentile(samples: list[int], percentile_value: float) -> int:
    """Return the conservative nearest-rank percentile for integer samples."""
    if not samples:
        return 0
    ordered = sorted(samples)
    index = max(
        0,
        min(
            len(ordered) - 1,
            math.ceil(percentile_value * len(ordered)) - 1,
        ),
    )
    return int(ordered[index])


__all__ = ["percentile"]
