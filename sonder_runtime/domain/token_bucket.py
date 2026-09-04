"""Token bucket rate limiter.

Smooth rate limiting with configurable burst capacity.  Tokens refill
at a constant rate up to the bucket capacity.  Each ``acquire()`` call
consumes one (or more) tokens; when the bucket is empty, the call
reports how long to wait.

No I/O, no threading -- callers own synchronization and clock.
"""
from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AcquireResult:
    allowed: bool
    tokens_remaining: float
    retry_after: float


class TokenBucket:
    """Fixed-rate token bucket with burst capacity.

    ``capacity``: maximum tokens (burst size).
    ``refill_rate``: tokens added per second.
    """

    def __init__(
        self,
        capacity: float,
        refill_rate: float,
        *,
        initial: float | None = None,
    ) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        if refill_rate <= 0:
            raise ValueError("refill_rate must be positive")
        self._capacity = float(capacity)
        self._rate = float(refill_rate)
        self._tokens = float(initial if initial is not None else capacity)
        self._last_refill: float | None = None

    def _refill(self, now: float) -> None:
        if self._last_refill is not None:
            elapsed = max(0.0, now - self._last_refill)
            self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
        self._last_refill = now

    def try_acquire(self, cost: float = 1.0, *, now: float | None = None) -> AcquireResult:
        ts = now if now is not None else time.monotonic()
        self._refill(ts)

        if self._tokens >= cost:
            self._tokens -= cost
            return AcquireResult(
                allowed=True,
                tokens_remaining=self._tokens,
                retry_after=0.0,
            )

        deficit = cost - self._tokens
        wait = deficit / self._rate
        return AcquireResult(
            allowed=False,
            tokens_remaining=self._tokens,
            retry_after=round(wait, 4),
        )

    def acquire(self, cost: float = 1.0, *, now: float | None = None) -> AcquireResult:
        result = self.try_acquire(cost, now=now)
        if not result.allowed:
            raise RateLimited(result.retry_after)
        return result

    @property
    def capacity(self) -> float:
        return self._capacity

    @property
    def tokens(self) -> float:
        return self._tokens

    @property
    def rate(self) -> float:
        return self._rate


class RateLimited(Exception):
    """Raised when a token bucket is exhausted."""

    def __init__(self, retry_after: float) -> None:
        self.retry_after = retry_after
        super().__init__(f"rate limited, retry after {retry_after:.2f}s")
