"""EWMA-smoothed fleet pressure with hysteresis banding.

Inspired by NVIDIA Personal-AI-Router's load-adaptive routing: raw
instantaneous metrics (active model calls, queue depth) are noisy and
cause oscillation when used directly for dispatch decisions.  An
exponentially weighted moving average smooths the signal, and hysteresis
bands with separate up/down thresholds prevent rapid toggling between
pressure levels.

No I/O, no threading primitives -- callers supply the raw sample and
receive a pure result.  The ``PressureTracker`` carries its own state
so each fleet owner gets an independent view.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field


BAND_LOW = "low"
BAND_MEDIUM = "medium"
BAND_HIGH = "high"
BAND_CRITICAL = "critical"
BANDS = (BAND_LOW, BAND_MEDIUM, BAND_HIGH, BAND_CRITICAL)

DEFAULT_ALPHA = 0.3
DEFAULT_THRESHOLDS: tuple[tuple[float, float], ...] = (
    (0.4, 0.3),
    (0.7, 0.6),
    (0.9, 0.85),
)


@dataclass
class PressureSample:
    ewma: float
    band: str
    raw: float
    capacity: int
    utilization: float
    ts: float


@dataclass
class PressureTracker:
    """Tracks fleet pressure as an EWMA over utilization ratio.

    ``alpha`` controls responsiveness: higher values weight recent samples
    more heavily (0.3 default balances ~3-sample half-life against noise).

    ``thresholds`` is a sequence of (up, down) pairs defining band
    boundaries.  The tracker moves UP when the EWMA crosses ``up``, but
    only moves DOWN when it drops below ``down`` -- the gap is the
    hysteresis that prevents oscillation at a boundary.
    """

    alpha: float = DEFAULT_ALPHA
    thresholds: tuple[tuple[float, float], ...] = DEFAULT_THRESHOLDS
    _ewma: float = 0.0
    _band: str = BAND_LOW
    _last_ts: float = 0.0
    _sample_count: int = 0

    def update(
        self,
        active: int,
        capacity: int,
        *,
        ts: float | None = None,
    ) -> PressureSample:
        now = ts if ts is not None else time.monotonic()
        cap = max(1, capacity)
        raw = min(1.0, max(0.0, active / cap))

        if self._sample_count == 0:
            self._ewma = raw
        else:
            self._ewma = self._alpha_blend(raw)
        self._sample_count += 1
        self._last_ts = now

        self._band = self._resolve_band(self._ewma, self._band)

        return PressureSample(
            ewma=round(self._ewma, 6),
            band=self._band,
            raw=round(raw, 6),
            capacity=cap,
            utilization=round(raw, 6),
            ts=now,
        )

    def current(self) -> PressureSample:
        return PressureSample(
            ewma=round(self._ewma, 6),
            band=self._band,
            raw=round(self._ewma, 6),
            capacity=0,
            utilization=round(self._ewma, 6),
            ts=self._last_ts,
        )

    def _alpha_blend(self, raw: float) -> float:
        return self.alpha * raw + (1.0 - self.alpha) * self._ewma

    def _resolve_band(self, ewma: float, current_band: str) -> str:
        band_index = BANDS.index(current_band) if current_band in BANDS else 0
        for i, (up, down) in enumerate(self.thresholds):
            boundary_index = i + 1
            if band_index < boundary_index and ewma >= up:
                band_index = boundary_index
            elif band_index >= boundary_index and ewma < down:
                band_index = boundary_index - 1
        return BANDS[min(band_index, len(BANDS) - 1)]

    @property
    def band(self) -> str:
        return self._band

    @property
    def ewma(self) -> float:
        return self._ewma

    @property
    def sample_count(self) -> int:
        return self._sample_count


def admission_factor(band: str) -> float:
    """Scaling factor for new work admission based on pressure band.

    Returns 1.0 at low pressure (full admission), decreasing through
    medium and high, and 0.0 at critical (shed all new work).
    """
    return {
        BAND_LOW: 1.0,
        BAND_MEDIUM: 0.7,
        BAND_HIGH: 0.3,
        BAND_CRITICAL: 0.0,
    }.get(band, 1.0)
