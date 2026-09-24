"""Host-owned strategy facts; no inference, IO, or permission expansion.

Signatures identify an explicitly declared hypothesis and change, rather than
trying to infer equivalence between arbitrary command strings. Evidence payloads
stay in their original stores; this boundary keeps only bounded identities.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass, field, fields
from enum import Enum


class StrategyError(ValueError):
    """Malformed, ambiguous, or out-of-budget strategy data."""


def _text(value, name, maximum=512):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise StrategyError(f"{name} must be bounded non-empty text")
    if any(ord(c) < 32 for c in value):
        raise StrategyError(f"{name} contains control characters")
    return value.strip()


def _digest(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
        raise StrategyError("canonical SHA-256 digest required")
    return value


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=True, allow_nan=False).encode("ascii")).hexdigest()


def _tuple(value, kind, name, maximum=64):
    if type(value) is not tuple or len(value) > maximum or any(not isinstance(x, kind) for x in value):
        raise StrategyError(f"{name} requires a bounded typed tuple")


class FailureClass(str, Enum):
    TRANSIENT_TRANSPORT = "transient_transport"
    RATE_LIMIT = "rate_limit"
    MODEL_UNAVAILABLE = "model_unavailable"
    INVALID_TOOL_ARGUMENT = "invalid_tool_argument"
    TOOL_NOT_FOUND = "tool_not_found"
    PERMISSION_DENIED = "permission_denied"
    TARGET_NOT_FOUND = "target_not_found"
    STALE_EVIDENCE = "stale_evidence"
    STATE_CHANGED = "state_changed"
    BUILD_FAILURE = "build_failure"
    TEST_FAILURE = "test_failure"
    VERIFIER_FAILURE = "verifier_failure"
    IMPLEMENTATION_FAILURE = "implementation_failure"
    HYPOTHESIS_REJECTED = "hypothesis_rejected"
    CONTEXT_EXHAUSTION = "context_exhaustion"
    RESOURCE_PRESSURE = "resource_pressure"
    TIME_BUDGET = "time_budget"
    DUPLICATE_STRATEGY = "duplicate_strategy"
    NO_PROGRESS = "no_progress"
    DEPENDENCY_FAILURE = "dependency_failure"
    ENVIRONMENT_FAILURE = "environment_failure"
    UNCERTAIN_SIDE_EFFECT = "uncertain_side_effect"
    RECOVERY_REQUIRED = "recovery_required"
    POLICY_BLOCK = "policy_block"
    OPERATOR_REQUIRED = "operator_required"


@dataclass(frozen=True, slots=True)
class FailureObservation:
    classification: FailureClass
    source_code: str = ""
    evidence_digest: str = ""

    def __post_init__(self):
        if not isinstance(self.classification, FailureClass):
            raise StrategyError("typed failure class required")
        if self.source_code:
            _text(self.source_code, "source_code", 256)
        if self.evidence_digest:
            _digest(self.evidence_digest)

    @property
    def requires_reconciliation(self):
        return self.classification in {FailureClass.UNCERTAIN_SIDE_EFFECT, FailureClass.RECOVERY_REQUIRED}

    @property
    def transport_retryable(self):
        return self.classification in {FailureClass.TRANSIENT_TRANSPORT, FailureClass.RATE_LIMIT}

    @property
    def requires_reinspection(self):
        return self.classification in {FailureClass.STALE_EVIDENCE, FailureClass.STATE_CHANGED,
                                       FailureClass.TARGET_NOT_FOUND}

    @property
    def replay_safe(self):
        # Classification is not proof that a mutating call is idempotent.
        # Even transport failures require the effect boundary's separate proof.
        return False

    @property
    def requires_new_strategy(self):
        return not self.transport_retryable and not self.requires_reconciliation


class ProgressAssessment(str, Enum):
    IMPROVED = "improved"
    NEUTRAL = "neutral"
    REGRESSED = "regressed"
    STALLED = "stalled"
    INCOMPARABLE = "incomparable"


@dataclass(frozen=True, slots=True)
class ProgressMetric:
    name: str
    value: float
    direction: str = "minimize"

    def __post_init__(self):
        _text(self.name, "metric", 128)
        if type(self.value) not in (int, float) or not math.isfinite(self.value) or self.value < 0:
            raise StrategyError("progress requires finite non-negative measurements")
        if self.direction not in ("minimize", "maximize"):
            raise StrategyError("unknown progress direction")


@dataclass(frozen=True, slots=True)
class ProgressVector:
    scope_digest: str
    metrics: tuple[ProgressMetric, ...]
    complete: bool = True

    def __post_init__(self):
        _digest(self.scope_digest)
        _tuple(self.metrics, ProgressMetric, "metrics", 32)
        if not self.metrics or len({x.name for x in self.metrics}) != len(self.metrics):
            raise StrategyError("progress requires unique measurements")
        if type(self.complete) is not bool:
            raise StrategyError("complete must be boolean")
        object.__setattr__(self, "metrics", tuple(sorted(self.metrics, key=lambda x: x.name)))


def assess_progress(before: ProgressVector | None, after: ProgressVector | None):
    if before is None or after is None or not before.complete or not after.complete:
        return ProgressAssessment.INCOMPARABLE
    if before.scope_digest != after.scope_digest:
        return ProgressAssessment.INCOMPARABLE
    if tuple((x.name, x.direction) for x in before.metrics) != tuple((x.name, x.direction) for x in after.metrics):
        return ProgressAssessment.INCOMPARABLE
    changes = [(b.value - a.value) * (1 if a.direction == "minimize" else -1)
               for a, b in zip(before.metrics, after.metrics)]
    if any(x > 0 for x in changes):
        return ProgressAssessment.REGRESSED
    if any(x < 0 for x in changes):
        return ProgressAssessment.IMPROVED
    return ProgressAssessment.NEUTRAL


@dataclass(frozen=True, slots=True)
class EvidenceRef:
    kind: str
    reference: str
    digest: str

    def __post_init__(self):
        if self.kind not in {"verifier", "effect", "artifact", "memory", "skill", "context", "workspace", "route"}:
            raise StrategyError("unknown evidence kind")
        _text(self.reference, "evidence reference")
        _digest(self.digest)


FAMILIES = frozenset({"inspect", "retrieve", "diagnose", "patch", "refactor", "configure",
    "dependency_change", "rollback", "retry_transport", "alternate_tool", "alternate_model",
    "critic_review", "parallel_hypotheses", "task_replan", "environment_repair", "delegate", "escalate"})


@dataclass(frozen=True, slots=True)
class StrategySignature:
    family: str
    objective_digest: str
    target_scope: tuple[str, ...]
    hypothesis_digest: str
    intended_change: str
    verifier_target: str

    def __post_init__(self):
        if self.family not in FAMILIES:
            raise StrategyError("unknown strategy family")
        _digest(self.objective_digest)
        _digest(self.hypothesis_digest)
        _tuple(self.target_scope, str, "target_scope", 32)
        clean = tuple(sorted(_text(x, "scope") for x in self.target_scope))
        if not clean or len(set(clean)) != len(clean):
            raise StrategyError("strategy scope must be non-empty and unique")
        object.__setattr__(self, "target_scope", clean)
        object.__setattr__(self, "intended_change", " ".join(_text(self.intended_change, "intended_change").split()))
        object.__setattr__(self, "verifier_target", _text(self.verifier_target, "verifier_target"))

    @property
    def digest(self):
        return _hash({"schema": 1, **asdict(self)})


@dataclass(frozen=True, slots=True)
class StrategyUsage:
    attempts: int = 0
    model_calls: int = 0
    tool_calls: int = 0
    verifier_calls: int = 0
    tokens: int = 0
    wall_seconds: float = 0
    descendants: int = 0
    strategy_switches: int = 0
    replans: int = 0
    critic_calls: int = 0
    top_tier_calls: int = 0

    def __post_init__(self):
        for dimension in fields(self):
            value = getattr(self, dimension.name)
            numeric = type(value) in (int, float) if dimension.name == "wall_seconds" else type(value) is int
            if not numeric or not math.isfinite(value) or not 0 <= value <= 10**12:
                raise StrategyError(f"invalid resource value: {dimension.name}")

    def plus(self, other: StrategyUsage):
        return StrategyUsage(**{f.name: getattr(self, f.name) + getattr(other, f.name) for f in fields(self)})


@dataclass(frozen=True, slots=True)
class StrategyBudget(StrategyUsage):
    attempts: int = 6
    model_calls: int = 12
    tool_calls: int = 64
    verifier_calls: int = 12
    tokens: int = 65536
    wall_seconds: float = 900
    descendants: int = 8
    strategy_switches: int = 4
    replans: int = 2
    critic_calls: int = 2
    top_tier_calls: int = 1

    def allows(self, usage: StrategyUsage):
        return all(getattr(usage, f.name) <= getattr(self, f.name) for f in fields(self))

    def remaining(self, usage: StrategyUsage):
        if not self.allows(usage):
            raise StrategyError("strategy budget exceeded")
        return StrategyBudget(**{f.name: getattr(self, f.name) - getattr(usage, f.name) for f in fields(self)})

    def reserve(self, child: StrategyBudget):
        return self.remaining(child)


class StrategyAction(str, Enum):
    RETRY_TRANSIENT = "retry_transient"
    RETRY_MODIFIED = "retry_modified"
    INSPECT = "inspect"
    RETRIEVE = "retrieve"
    REPAIR = "repair"
    SWITCH_TOOL = "switch_tool"
    SWITCH_MODEL = "switch_model"
    CRITIC = "critic"
    SPAWN_SPECIALIST = "spawn_specialist"
    PARALLEL_HYPOTHESES = "parallel_hypotheses"
    REPLAN = "replan"
    ROLLBACK = "rollback"
    RECONCILE = "reconcile"
    PAUSE = "pause"
    FAIL = "fail"


@dataclass(frozen=True, slots=True)
class StrategyAttempt:
    run_id: str
    attempt_id: str
    signature: StrategySignature
    outcome: str
    failure: FailureObservation | None = None
    progress_before: ProgressVector | None = None
    progress_after: ProgressVector | None = None
    usage: StrategyUsage = field(default_factory=StrategyUsage)
    evidence: tuple[EvidenceRef, ...] = ()
    parent_attempt_id: str = ""
    model_route: str = ""
    created_at: str = ""

    def __post_init__(self):
        _text(self.run_id, "run_id", 128)
        _text(self.attempt_id, "attempt_id", 128)
        if not isinstance(self.signature, StrategySignature) or not isinstance(self.usage, StrategyUsage):
            raise StrategyError("typed strategy signature and usage required")
        if self.outcome not in {"succeeded", "failed", "paused", "uncertain"}:
            raise StrategyError("unknown strategy outcome")
        if self.failure is not None and not isinstance(self.failure, FailureObservation):
            raise StrategyError("typed failure required")
        if self.outcome == "succeeded" and self.failure is not None:
            raise StrategyError("successful attempt cannot carry a failure")
        for value in (self.progress_before, self.progress_after):
            if value is not None and not isinstance(value, ProgressVector):
                raise StrategyError("typed progress required")
        _tuple(self.evidence, EvidenceRef, "evidence")
        identities = [(x.kind, x.reference) for x in self.evidence]
        if len(set(identities)) != len(identities):
            raise StrategyError("duplicate or conflicting evidence reference")
        object.__setattr__(self, "evidence", tuple(sorted(self.evidence, key=lambda x: (x.kind, x.reference))))
        for name in ("parent_attempt_id", "model_route", "created_at"):
            if getattr(self, name):
                _text(getattr(self, name), name, 128)
        if self.parent_attempt_id == self.attempt_id:
            raise StrategyError("attempt cannot parent itself")

    def as_dict(self):
        return asdict(self)

    @property
    def digest(self):
        return _hash(self.as_dict())

    @classmethod
    def from_dict(cls, value):
        try:
            if not isinstance(value, dict) or set(value) != {f.name for f in fields(cls)}:
                raise StrategyError("exact strategy attempt envelope required")
            body = dict(value)
            body["signature"] = StrategySignature(**{**body["signature"], "target_scope": tuple(body["signature"]["target_scope"])})
            if body["failure"] is not None:
                body["failure"] = FailureObservation(**{**body["failure"], "classification": FailureClass(body["failure"]["classification"])})
            for name in ("progress_before", "progress_after"):
                if body[name] is not None:
                    body[name] = ProgressVector(**{**body[name], "metrics": tuple(ProgressMetric(**x) for x in body[name]["metrics"])})
            body["usage"] = StrategyUsage(**body["usage"])
            body["evidence"] = tuple(EvidenceRef(**x) for x in body["evidence"])
            return cls(**body)
        except (KeyError, TypeError, ValueError) as exc:
            raise StrategyError("invalid strategy attempt") from exc
