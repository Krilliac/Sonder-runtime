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
    # How long a client address that logged in successfully as a username
    # stays "familiar" and bypasses that username's backoff, so a remote
    # attacker holding the window open cannot lock the real user out.  The
    # per-IP limiter still applies to familiar clients.
    familiar_ttl_seconds: float = 30.0 * 24 * 3600
    max_familiar: int = 4096

    def __post_init__(self) -> None:
        if (
            self.threshold < 1 or self.decay_seconds <= 0 or self.max_entries < 1
            or self.familiar_ttl_seconds <= 0 or self.max_familiar < 1
        ):
            raise ValueError("login throttle policy values must be positive")


@dataclass(frozen=True)
class LoginDecision:
    allowed: bool
    retry_after_seconds: int = 0


ALLOWED = LoginDecision(True, 0)


class UsernameBackoff:
    __slots__ = ("_policy", "_entries", "_familiar")

    def __init__(self, policy: LoginThrottlePolicy) -> None:
        self._policy = policy
        # key -> (failure count as of `stamp`, stamp); insertion order is
        # least-recently-touched first.
        self._entries: dict[str, tuple[float, float]] = {}
        # (key, client) -> time of last successful login; same ordering.
        self._familiar: dict[tuple[str, str], float] = {}

    def is_familiar(self, key: str, client: str, now: float) -> bool:
        if not client:
            return False
        seen = self._familiar.get((key, client))
        return seen is not None and now - seen < self._policy.familiar_ttl_seconds

    def __len__(self) -> int:
        return len(self._entries)

    def _current(self, key: str, now: float) -> float:
        entry = self._entries.get(key)
        if entry is None:
            return 0.0
        count, stamp = entry
        return max(0.0, count - max(0.0, now - stamp) / self._policy.decay_seconds)

    def check(
        self, key: str, now: float, *, client: str = "", trusted: bool = False
    ) -> LoginDecision:
        """Decide whether an attempt may proceed.

        ``trusted`` (a loopback client) and a familiar ``client`` bypass the
        window: the operator always has a way in that a remote attacker
        sustaining failures cannot hold.
        """
        if trusted or self.is_familiar(key, client, now):
            return ALLOWED
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

    def record_success(self, key: str, now: float = 0.0, client: str = "") -> None:
        self._entries.pop(key, None)
        if client:
            self._familiar.pop((key, client), None)
            self._familiar[(key, client)] = now
            while len(self._familiar) > self._policy.max_familiar:
                del self._familiar[next(iter(self._familiar))]

    def _evict(self, now: float) -> None:
        limit = self._policy.max_entries
        for stale in [k for k in self._entries if self._current(k, now) <= 0.0]:
            del self._entries[stale]
        # Entries in active backoff are the last to go: junk-username spray
        # must not reset a target's counter while quieter entries remain.
        floor = self._policy.threshold - 1
        for key in [k for k in self._entries if self._current(k, now) <= floor]:
            if len(self._entries) <= limit:
                return
            del self._entries[key]
        while len(self._entries) > limit:
            del self._entries[next(iter(self._entries))]
