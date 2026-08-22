"""Pure retry and side-effect policy for the WP2 application loop.

The policy describes what an adapter may do; it does not sleep, perform
reconciliation, or persist an idempotency record.  Those effects belong to
the caller's transport or repository boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math


class RetryClass(str, Enum):
    TRANSIENT = "transient"
    THROTTLED = "throttled"
    UNKNOWN_OUTCOME = "unknown_outcome"
    PERMANENT = "permanent"


class SideEffectClass(str, Enum):
    NONE = "none"
    IDEMPOTENT = "idempotent"
    NON_IDEMPOTENT = "non_idempotent"


class ReplayAction(str, Enum):
    RETRY = "retry"
    RECONCILE_THEN_RETRY = "reconcile_then_retry"
    DO_NOT_RETRY = "do_not_retry"


@dataclass(frozen=True)
class BackoffMetadata:
    """The delay contract an adapter should apply before another attempt."""

    strategy: str = "exponential_full_jitter"
    base_seconds: float = 0.25
    maximum_seconds: float = 30.0
    retry_after_seconds: float | None = None
    deadline_seconds: float | None = None

    def __post_init__(self) -> None:
        if self.base_seconds <= 0 or self.maximum_seconds <= 0:
            raise ValueError("backoff bounds must be positive")
        if self.base_seconds > self.maximum_seconds:
            raise ValueError("base_seconds cannot exceed maximum_seconds")
        for value in (self.retry_after_seconds, self.deadline_seconds):
            if value is not None and (not math.isfinite(value) or value < 0):
                raise ValueError("backoff hints must be finite and non-negative")

    def cap_for_attempt(self, attempt: int) -> float:
        """Return the capped exponential range; jitter is applied by the adapter."""
        if attempt < 1:
            raise ValueError("attempt must be positive")
        if attempt > 1 + int(math.log2(self.maximum_seconds / self.base_seconds)):
            return self.maximum_seconds
        return min(self.maximum_seconds, self.base_seconds * (2 ** (attempt - 1)))


@dataclass(frozen=True)
class SideEffectRequirement:
    """Replay requirements for the operation's side-effect boundary."""

    effect: SideEffectClass
    idempotency_key_required: bool
    idempotency_key_present: bool
    reconciliation_required: bool
    requirement: str


@dataclass(frozen=True)
class RetryDecision:
    classification: RetryClass
    action: ReplayAction
    attempt_limit: int
    backoff: BackoffMetadata
    side_effect: SideEffectRequirement
    reason: str


def classify_retry(
    failure_code: str | None = None,
    *,
    status: int | None = None,
    outcome_known: bool = True,
) -> RetryClass:
    """Classify explicit transport/application signals without inspecting exceptions.

    A missing response after dispatch is an unknown outcome, even when the
    transport reported a timeout.  That distinction prevents a blind replay
    from duplicating a side effect.
    """
    if not outcome_known:
        return RetryClass.UNKNOWN_OUTCOME
    code = str(failure_code or "").strip().casefold().replace("-", "_")
    if status in {408, 425, 429, 500, 502, 503, 504} or code in {
        "timeout", "timed_out", "temporarily_unavailable", "connection_reset",
        "connection_refused", "overloaded", "transport_unavailable",
    }:
        return RetryClass.THROTTLED if status == 429 or code == "overloaded" else RetryClass.TRANSIENT
    return RetryClass.PERMANENT


def side_effect_requirement(
    effect: SideEffectClass | str,
    *,
    outcome_known: bool,
    idempotency_key: str | None = None,
) -> SideEffectRequirement:
    """Describe the proof required before replaying an operation."""
    effect = SideEffectClass(effect)
    key_present = bool(str(idempotency_key or "").strip())
    if effect is SideEffectClass.NONE:
        return SideEffectRequirement(effect, False, False, False, "no side effect to reconcile")
    if effect is SideEffectClass.IDEMPOTENT:
        return SideEffectRequirement(
            effect, True, key_present, not outcome_known,
            "reuse the same idempotency key and reconcile an unknown outcome",
        )
    return SideEffectRequirement(
        effect, True, key_present, True,
        "reconcile the prior effect before replay; a unique idempotency key is required",
    )


def retry_decision(
    failure_code: str | None = None,
    *,
    status: int | None = None,
    attempt: int = 1,
    max_attempts: int = 3,
    outcome_known: bool = True,
    effect: SideEffectClass | str = SideEffectClass.NONE,
    idempotency_key: str | None = None,
    retry_after_seconds: float | None = None,
    deadline_seconds: float | None = None,
) -> RetryDecision:
    """Build bounded replay metadata for one failed loop attempt."""
    if attempt < 1 or max_attempts < 1:
        raise ValueError("attempt and max_attempts must be positive")
    classification = classify_retry(failure_code, status=status, outcome_known=outcome_known)
    side_effect = side_effect_requirement(
        effect, outcome_known=outcome_known, idempotency_key=idempotency_key,
    )
    if classification is RetryClass.UNKNOWN_OUTCOME:
        action = ReplayAction.RECONCILE_THEN_RETRY if side_effect.reconciliation_required else ReplayAction.RETRY
    else:
        action = (
            ReplayAction.RETRY
            if classification in {RetryClass.TRANSIENT, RetryClass.THROTTLED}
            else ReplayAction.DO_NOT_RETRY
        )
    if side_effect.effect is SideEffectClass.NON_IDEMPOTENT and not outcome_known:
        action = ReplayAction.RECONCILE_THEN_RETRY
    if side_effect.effect is not SideEffectClass.NONE and not side_effect.idempotency_key_present:
        action = ReplayAction.DO_NOT_RETRY
    if deadline_seconds == 0:
        action = ReplayAction.DO_NOT_RETRY
    if attempt >= max_attempts and action is ReplayAction.RETRY:
        action = ReplayAction.DO_NOT_RETRY
    backoff = BackoffMetadata(
        retry_after_seconds=retry_after_seconds, deadline_seconds=deadline_seconds,
    )
    return RetryDecision(
        classification, action, max_attempts, backoff, side_effect,
        f"{classification.value}; attempt {attempt} of {max_attempts}",
    )


__all__ = [
    "BackoffMetadata", "ReplayAction", "RetryClass", "RetryDecision",
    "SideEffectClass", "SideEffectRequirement", "classify_retry",
    "retry_decision", "side_effect_requirement",
]
