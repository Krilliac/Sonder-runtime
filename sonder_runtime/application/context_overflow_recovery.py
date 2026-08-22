"""Application policy for bounded context-overflow recovery (WP4 CTX-007).

This module owns orchestration only.  Context construction, token accounting,
and the actual compaction/shrinking algorithms are supplied by callers.  That
keeps the recovery boundary usable by different context providers without
changing the existing context modules.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Callable, Generic, TypeVar


T = TypeVar("T")
Compactor = Callable[[T], T | None]
Shrinker = Callable[[T, float], T | None]
Fits = Callable[[T], bool]


@dataclass(frozen=True)
class RecoveryResult(Generic[T]):
    """One bounded recovery decision."""

    candidate: T | None
    action: str
    attempts: int = 0
    used_last_good: bool = False


class ContextOverflowRecovery(Generic[T]):
    """Coordinate preflight compaction, bounded retry shrinking, and fallback.

    ``max_attempts`` counts post-overflow candidates, not the original request.
    A compaction candidate is tried first, followed by at most that many
    shrink steps.  Each step receives a strictly smaller target factor, and the
    policy never loops or retries an unclassified failure.
    """

    def __init__(
        self,
        *,
        compact: Compactor[T],
        shrink: Shrinker[T],
        max_attempts: int = 3,
        shrink_factor: float = 0.75,
        preflight_ratio: float = 0.90,
    ) -> None:
        if not callable(compact) or not callable(shrink):
            raise TypeError("compact and shrink must be callable")
        if not isinstance(max_attempts, int) or not 1 <= max_attempts <= 8:
            raise ValueError("max_attempts must be between 1 and 8")
        if not 0.0 < shrink_factor < 1.0:
            raise ValueError("shrink_factor must be between 0 and 1")
        if not 0.0 < preflight_ratio <= 1.0:
            raise ValueError("preflight_ratio must be between 0 and 1")
        self._compact = compact
        self._shrink = shrink
        self.max_attempts = max_attempts
        self.shrink_factor = shrink_factor
        self.preflight_ratio = preflight_ratio
        self._last_good: T | None = None

    def accept(self, candidate: T) -> None:
        """Publish a complete successful view as the new last-good snapshot."""
        self._last_good = copy.deepcopy(candidate)

    def last_good(self) -> T | None:
        """Return an isolated last-good snapshot, if one exists."""
        return copy.deepcopy(self._last_good)

    def prepare(
        self,
        candidate: T,
        *,
        estimated_tokens: int,
        context_limit: int,
        reserved_output_tokens: int = 0,
    ) -> RecoveryResult[T]:
        """Compact before overflow when the projected request reaches the boundary."""
        if context_limit <= 0 or estimated_tokens < 0 or reserved_output_tokens < 0:
            raise ValueError("token values must be non-negative and limit must be positive")
        threshold = int(context_limit * self.preflight_ratio)
        projected = estimated_tokens + reserved_output_tokens
        if projected < threshold:
            return RecoveryResult(candidate, "unchanged")
        compacted = self._compact(copy.deepcopy(candidate))
        if compacted is None:
            return RecoveryResult(candidate, "preflight_unavailable")
        return RecoveryResult(compacted, "preflight_compacted", attempts=1)

    def recover(
        self,
        candidate: T,
        *,
        overflow: bool,
        fits: Fits[T],
    ) -> RecoveryResult[T]:
        """Recover only from a proven overflow, falling back to last-good.

        ``fits`` is evaluated at most ``max_attempts + 1`` times.  A normal
        (non-overflow) failure is returned unchanged and is never retried.
        Successful candidates become last-good snapshots.
        """
        if not overflow:
            return RecoveryResult(candidate, "not_overflow")
        if not callable(fits):
            raise TypeError("fits must be callable")

        current = copy.deepcopy(candidate)
        compacted = self._compact(current)
        attempts = 1
        if compacted is not None and fits(compacted):
            self.accept(compacted)
            return RecoveryResult(compacted, "compacted", attempts)

        current = compacted if compacted is not None else current
        for step in range(1, self.max_attempts + 1):
            factor = self.shrink_factor**step
            smaller = self._shrink(copy.deepcopy(current), factor)
            attempts += 1
            if smaller is None:
                continue
            current = smaller
            if fits(smaller):
                self.accept(smaller)
                return RecoveryResult(smaller, "adaptively_shrunk", attempts)

        fallback = self.last_good()
        return RecoveryResult(fallback, "last_good" if fallback is not None else "unrecoverable", attempts, fallback is not None)
