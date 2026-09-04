"""Circuit breaker state machine for external service calls.

Three states: CLOSED (normal), OPEN (fail-fast), HALF_OPEN (probe).

In CLOSED state, failures are tracked in a sliding window.  When the
failure count within the window exceeds the threshold, the breaker
trips to OPEN.  After a recovery timeout, it transitions to HALF_OPEN
and allows a single probe call.  If the probe succeeds, it resets to
CLOSED; if it fails, it returns to OPEN with an exponentially
increasing recovery timeout.

No I/O, no threading primitives -- callers own synchronization.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum


class BreakerState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class BreakerConfig:
    failure_threshold: int = 5
    window_seconds: float = 60.0
    recovery_seconds: float = 30.0
    max_recovery_seconds: float = 300.0
    recovery_multiplier: float = 2.0
    half_open_max_probes: int = 1


@dataclass
class BreakerSnapshot:
    state: BreakerState
    failure_count: int
    success_count: int
    consecutive_failures: int
    trip_count: int
    last_failure_ts: float | None
    recovery_deadline: float | None


class CircuitBreaker:
    """Pure state machine for circuit breaking.

    Call ``before_call()`` to check if a call is allowed.
    Call ``record_success()`` or ``record_failure()`` after the call.
    """

    def __init__(self, config: BreakerConfig | None = None) -> None:
        self._cfg = config or BreakerConfig()
        self._state = BreakerState.CLOSED
        self._failures: list[float] = []
        self._consecutive_failures = 0
        self._success_count = 0
        self._trip_count = 0
        self._last_failure_ts: float | None = None
        self._recovery_deadline: float | None = None
        self._current_recovery = self._cfg.recovery_seconds
        self._half_open_probes = 0

    def before_call(self, *, now: float | None = None) -> bool:
        ts = now if now is not None else time.monotonic()

        if self._state == BreakerState.CLOSED:
            return True

        if self._state == BreakerState.OPEN:
            if self._recovery_deadline is not None and ts >= self._recovery_deadline:
                self._state = BreakerState.HALF_OPEN
                self._half_open_probes = 0
                return True
            return False

        if self._state == BreakerState.HALF_OPEN:
            return self._half_open_probes < self._cfg.half_open_max_probes

        return False

    def record_success(self, *, now: float | None = None) -> BreakerState:
        ts = now if now is not None else time.monotonic()
        self._success_count += 1

        if self._state == BreakerState.HALF_OPEN:
            self._state = BreakerState.CLOSED
            self._failures.clear()
            self._consecutive_failures = 0
            self._current_recovery = self._cfg.recovery_seconds
            self._recovery_deadline = None
            self._half_open_probes = 0
        elif self._state == BreakerState.CLOSED:
            self._consecutive_failures = 0

        return self._state

    def record_failure(self, *, now: float | None = None) -> BreakerState:
        ts = now if now is not None else time.monotonic()
        self._consecutive_failures += 1
        self._last_failure_ts = ts

        if self._state == BreakerState.HALF_OPEN:
            self._half_open_probes += 1
            self._trip(ts)
            return self._state

        if self._state == BreakerState.CLOSED:
            self._failures.append(ts)
            cutoff = ts - self._cfg.window_seconds
            self._failures = [t for t in self._failures if t > cutoff]
            if len(self._failures) >= self._cfg.failure_threshold:
                self._trip(ts)

        return self._state

    def _trip(self, ts: float) -> None:
        self._state = BreakerState.OPEN
        self._trip_count += 1
        self._recovery_deadline = ts + self._current_recovery
        self._current_recovery = min(
            self._current_recovery * self._cfg.recovery_multiplier,
            self._cfg.max_recovery_seconds,
        )

    def reset(self) -> None:
        self._state = BreakerState.CLOSED
        self._failures.clear()
        self._consecutive_failures = 0
        self._recovery_deadline = None
        self._current_recovery = self._cfg.recovery_seconds
        self._half_open_probes = 0

    @property
    def state(self) -> BreakerState:
        return self._state

    @property
    def trip_count(self) -> int:
        return self._trip_count

    def snapshot(self) -> BreakerSnapshot:
        return BreakerSnapshot(
            state=self._state,
            failure_count=len(self._failures),
            success_count=self._success_count,
            consecutive_failures=self._consecutive_failures,
            trip_count=self._trip_count,
            last_failure_ts=self._last_failure_ts,
            recovery_deadline=self._recovery_deadline,
        )
