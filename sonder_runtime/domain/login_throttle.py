"""Per-username login failure backoff (I/O-free, clock injected).

A token-style counter per normalised username: every failed guess adds one,
and the count decays linearly with time.  At or above ``threshold`` the
username is in a backoff window and further attempts are refused until enough
decay has passed for one more attempt.  Refused attempts do not add to the
count, so an attacker cannot extend the window by hammering it: the window
never exceeds ``decay_seconds`` past the point the threshold was reached, and
a correct password clears the counter.  This is a delay, never a permanent
lock.

Not thread-safe: the owning adapter serialises calls.  Memory is bounded by
``max_entries``; when full, entries that have decayed to zero go first, then
the least recently touched.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class LoginThrottlePolicy:
    threshold: int = 20
    decay_seconds: float = 30.0
    max_entries: int = 4096

    def __post_init__(self) -> None:
        if self.threshold < 1 or self.decay_seconds <= 0 or self.max_entries < 1:
            raise ValueError("login throttle policy values must be positive")


@dataclass(frozen=True)
class LoginDecision:
    allowed: bool
    retry_after_seconds: int = 0


ALLOWED = LoginDecision(True, 0)


class UsernameBackoff:
    __slots__ = ("_policy", "_entries")

    def __init__(self, policy: LoginThrottlePolicy) -> None:
        self._policy = policy
        # key -> (failure count as of `stamp`, stamp); insertion order is
        # least-recently-touched first.
        self._entries: dict[str, tuple[float, float]] = {}

    def __len__(self) -> int:
        return len(self._entries)

    def _current(self, key: str, now: float) -> float:
        entry = self._entries.get(key)
        if entry is None:
            return 0.0
        count, stamp = entry
        return max(0.0, count - max(0.0, now - stamp) / self._policy.decay_seconds)

    def check(self, key: str, now: float) -> LoginDecision:
        excess = self._current(key, now) - (self._policy.threshold - 1)
        if excess <= 0.0:
            return ALLOWED
        return LoginDecision(
            False, max(1, math.ceil(excess * self._policy.decay_seconds))
        )

    def record_failure(self, key: str, now: float) -> None:
        count = self._current(key, now) + 1.0
        self._entries.pop(key, None)
        self._entries[key] = (count, now)
        if len(self._entries) > self._policy.max_entries:
            self._evict(now)

    def record_success(self, key: str) -> None:
        self._entries.pop(key, None)

    def _evict(self, now: float) -> None:
        for stale in [k for k in self._entries if self._current(k, now) <= 0.0]:
            del self._entries[stale]
        while len(self._entries) > self._policy.max_entries:
            del self._entries[next(iter(self._entries))]
