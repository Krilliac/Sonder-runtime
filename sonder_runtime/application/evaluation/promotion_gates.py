"""Mechanical promotion gates with explicit thresholds and confidence bounds.

EVAL-007 asks for thresholds *and confidence requirements* for every class of
promotion.  ``RegressionThresholds`` (``reproducible``) gates one run against
point estimates; it cannot tell 3/3 from 300/300.  This module adds the
missing statistical floor and a per-kind policy table:

* :class:`PromotionKind` enumerates runtime, prompt, skill, route, model,
  memory, and selfmod promotion.
* :class:`PromotionGatePolicy` binds each kind to a minimum sample size, a
  point pass-rate floor, a one-sided Wilson score lower bound at a stated
  confidence level, baseline regression allowances, replay equivalence, and
  shadow/canary requirements.
* :func:`evaluate_promotion_gate` is a pure, deterministic function from
  recorded :class:`EvaluationResult` values and observations to a
  :class:`PromotionGateDecision` whose named ``gate_results`` feed directly
  into ``PromotionEvidence`` -- so a failed gate makes the evidence
  unacceptable and ``ProposalLifecycle.approve`` refuses it.

No model is consulted and no subjective judgment is accepted: every gate is a
comparison of recorded counts against the policy.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
import math
from statistics import NormalDist
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from .proposal_lifecycle import EvaluationMode, EvaluationResult, ShadowCanaryObservation


SCHEMA = "sonder.evaluation-promotion-gate.v1"
PASS_RATE_METRIC = "pass_rate"
_INTEGRAL_TOLERANCE = 1e-6
MAX_RESULTS = 256


class PromotionGateError(ValueError):
    """Invalid policy or evidence that cannot be gated mechanically."""


class PromotionKind(str, Enum):
    RUNTIME = "runtime"
    PROMPT = "prompt"
    SKILL = "skill"
    ROUTE = "route"
    MODEL = "model"
    MEMORY = "memory"
    SELFMOD = "selfmod"


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _rate(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1:
        raise PromotionGateError(f"{label} must be a finite rate in [0, 1]")
    return float(value)


def _count(value: Any, label: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise PromotionGateError(f"{label} must be an integer >= {minimum}")
    return value


def wilson_lower_bound(successes: int, samples: int, confidence: float) -> float:
    """One-sided Wilson score lower bound for a binomial pass rate."""
    _count(samples, "samples", minimum=1)
    _count(successes, "successes")
    if successes > samples:
        raise PromotionGateError("successes cannot exceed samples")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0.5 <= confidence < 1:
        raise PromotionGateError("confidence must be within [0.5, 1)")
    z = NormalDist().inv_cdf(float(confidence))
    p = successes / samples
    denominator = 1 + z * z / samples
    centre = p + z * z / (2 * samples)
    margin = z * math.sqrt(p * (1 - p) / samples + z * z / (4 * samples * samples))
    return max(0.0, (centre - margin) / denominator)


@dataclass(frozen=True)
class PromotionGatePolicy:
    """Thresholds and confidence requirements for one promotion kind."""

    kind: PromotionKind
    min_samples: int
    min_pass_rate: float
    min_pass_rate_lower_bound: float
    confidence: float
    max_case_regressions: int = 0
    max_pass_rate_drop: float = 0.0
    require_replay_equivalence: bool = True
    require_shadow: bool = True
    require_canary: bool = True
    min_canary_samples: int = 1
    schema: str = SCHEMA

    def __post_init__(self) -> None:
        if not isinstance(self.kind, PromotionKind):
            raise PromotionGateError("policy kind is invalid")
        if self.schema != SCHEMA:
            raise PromotionGateError("unsupported promotion gate schema")
        _count(self.min_samples, "min_samples", minimum=1)
        for name in ("min_pass_rate", "min_pass_rate_lower_bound", "max_pass_rate_drop"):
            object.__setattr__(self, name, _rate(getattr(self, name), name))
        if isinstance(self.confidence, bool) or not isinstance(self.confidence, (int, float)) or not 0.5 <= self.confidence < 1:
            raise PromotionGateError("confidence must be within [0.5, 1)")
        object.__setattr__(self, "confidence", float(self.confidence))
        if self.min_pass_rate_lower_bound > self.min_pass_rate:
            raise PromotionGateError("the confidence lower bound cannot exceed the point pass-rate floor")
        _count(self.max_case_regressions, "max_case_regressions")
        _count(self.min_canary_samples, "min_canary_samples", minimum=1)
        for name in ("require_replay_equivalence", "require_shadow", "require_canary"):
            if type(getattr(self, name)) is not bool:
                raise PromotionGateError(f"{name} must be boolean")
        if self.require_canary and not self.require_shadow:
            raise PromotionGateError("a canary requirement also requires a shadow observation")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "kind": self.kind.value,
            "min_samples": self.min_samples,
            "min_pass_rate": self.min_pass_rate,
            "min_pass_rate_lower_bound": self.min_pass_rate_lower_bound,
            "confidence": self.confidence,
            "max_case_regressions": self.max_case_regressions,
            "max_pass_rate_drop": self.max_pass_rate_drop,
            "require_replay_equivalence": self.require_replay_equivalence,
            "require_shadow": self.require_shadow,
            "require_canary": self.require_canary,
            "min_canary_samples": self.min_canary_samples,
        }

    @property
    def digest(self) -> str:
        return _digest(self.as_dict())


def _policy(kind: PromotionKind, samples: int, floor: float, bound: float, confidence: float, **extra: Any) -> PromotionGatePolicy:
    return PromotionGatePolicy(kind, samples, floor, bound, confidence, **extra)


# Defaults are deliberately conservative.  Every sample floor is at least the
# smallest count at which a perfect run can clear the stated Wilson bound (a
# test pins this), so the confidence gate is always attainable and no default
# can be satisfied by a handful of lucky cases.  Callers may supply a stricter
# table; the service refuses a table that omits any kind.
DEFAULT_PROMOTION_GATE_POLICIES: Mapping[PromotionKind, PromotionGatePolicy] = MappingProxyType({
    PromotionKind.RUNTIME: _policy(PromotionKind.RUNTIME, 60, 0.98, 0.95, 0.95),
    PromotionKind.PROMPT: _policy(PromotionKind.PROMPT, 30, 0.90, 0.80, 0.95, max_pass_rate_drop=0.02),
    PromotionKind.SKILL: _policy(PromotionKind.SKILL, 30, 0.90, 0.80, 0.95, max_pass_rate_drop=0.02),
    PromotionKind.ROUTE: _policy(PromotionKind.ROUTE, 30, 0.90, 0.80, 0.95, max_pass_rate_drop=0.02),
    PromotionKind.MODEL: _policy(PromotionKind.MODEL, 100, 0.90, 0.85, 0.95, max_pass_rate_drop=0.01),
    PromotionKind.MEMORY: _policy(PromotionKind.MEMORY, 30, 0.95, 0.85, 0.95),
    PromotionKind.SELFMOD: _policy(PromotionKind.SELFMOD, 60, 1.0, 0.95, 0.95),
})


def validate_policy_table(policies: Mapping[PromotionKind, PromotionGatePolicy]) -> Mapping[PromotionKind, PromotionGatePolicy]:
    """Require one well-formed policy for every promotion kind."""
    if not isinstance(policies, Mapping) or set(policies) != set(PromotionKind):
        raise PromotionGateError("a promotion gate policy is required for every promotion kind")
    for kind, policy in policies.items():
        if not isinstance(policy, PromotionGatePolicy) or policy.kind is not kind:
            raise PromotionGateError(f"policy for {kind.value} is missing or mislabelled")
    return MappingProxyType(dict(policies))


@dataclass(frozen=True)
class PromotionGateDecision:
    """Deterministic gate verdict with named sub-gates and reason codes."""

    kind: PromotionKind
    policy_digest: str
    samples: int
    successes: int
    pass_rate: float
    lower_bound: float
    replay_equivalent: bool
    gate_results: Mapping[str, bool]
    reason_codes: tuple[str, ...]
    result_ids: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.reason_codes and all(self.gate_results.values())

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "kind": self.kind.value,
            "policy_digest": self.policy_digest,
            "samples": self.samples,
            "successes": self.successes,
            "pass_rate": self.pass_rate,
            "lower_bound": self.lower_bound,
            "replay_equivalent": self.replay_equivalent,
            "gate_results": dict(self.gate_results),
            "reason_codes": list(self.reason_codes),
            "result_ids": list(self.result_ids),
            "passed": self.passed,
        }

    @property
    def digest(self) -> str:
        return _digest(self.as_dict())


def evaluate_promotion_gate(
    policy: PromotionGatePolicy,
    *,
    results: Sequence[EvaluationResult],
    baseline_pass_rate: float | None = None,
    case_regressions: int = 0,
    shadow: ShadowCanaryObservation | None = None,
    canary: ShadowCanaryObservation | None = None,
) -> PromotionGateDecision:
    """Gate offline results and observations against ``policy``.

    Offline results must carry a ``pass_rate`` metric whose product with
    ``sample_count`` is an integral success count; anything else is refused
    rather than rounded into a pass.  Samples are pooled across results.
    """
    if not isinstance(policy, PromotionGatePolicy):
        raise PromotionGateError("policy is invalid")
    if isinstance(results, (str, bytes)) or not isinstance(results, Sequence) or len(results) > MAX_RESULTS:
        raise PromotionGateError(f"results must be a sequence of at most {MAX_RESULTS} evaluation results")
    _count(case_regressions, "case_regressions")
    if baseline_pass_rate is not None:
        baseline_pass_rate = _rate(baseline_pass_rate, "baseline_pass_rate")
    offline: list[EvaluationResult] = []
    for result in results:
        if not isinstance(result, EvaluationResult):
            raise PromotionGateError("results must be EvaluationResult values")
        if result.mode is EvaluationMode.OFFLINE:
            offline.append(result)
    samples = 0
    successes = 0
    for result in offline:
        if PASS_RATE_METRIC not in result.metrics:
            raise PromotionGateError(f"offline result {result.result_id!r} lacks a {PASS_RATE_METRIC} metric")
        rate = _rate(result.metrics[PASS_RATE_METRIC], f"{result.result_id}.{PASS_RATE_METRIC}")
        raw = rate * result.sample_count
        passed_count = round(raw)
        if abs(raw - passed_count) > _INTEGRAL_TOLERANCE:
            raise PromotionGateError(f"offline result {result.result_id!r} pass_rate does not imply an integral success count")
        samples += result.sample_count
        successes += passed_count
    pass_rate = successes / samples if samples else 0.0
    lower_bound = wilson_lower_bound(successes, samples, policy.confidence) if samples else 0.0
    replay_equivalent = bool(offline) and all(item.replay_equivalent is True for item in offline)

    gates: dict[str, bool] = {
        "sample_size": samples >= policy.min_samples,
        "pass_rate": samples > 0 and pass_rate >= policy.min_pass_rate,
        "confidence_lower_bound": samples > 0 and lower_bound >= policy.min_pass_rate_lower_bound,
        "case_regressions": case_regressions <= policy.max_case_regressions,
        "pass_rate_drop": baseline_pass_rate is None or baseline_pass_rate - pass_rate <= policy.max_pass_rate_drop,
    }
    if policy.require_replay_equivalence:
        gates["replay_equivalence"] = replay_equivalent
    if policy.require_shadow:
        gates["shadow"] = shadow is not None and shadow.mode is EvaluationMode.SHADOW and shadow.healthy
    if policy.require_canary:
        gates["canary"] = (
            canary is not None and canary.mode is EvaluationMode.CANARY and canary.healthy
            and canary.sample_count >= policy.min_canary_samples
        )
    reasons = tuple(f"gate_failed:{name}" for name, ok in sorted(gates.items()) if not ok)
    if not offline:
        reasons = ("no_offline_results",) + reasons
    return PromotionGateDecision(
        policy.kind, policy.digest, samples, successes, pass_rate, lower_bound, replay_equivalent,
        MappingProxyType(dict(sorted(gates.items()))), reasons,
        tuple(sorted(item.result_id for item in offline)),
    )


__all__ = [
    "DEFAULT_PROMOTION_GATE_POLICIES", "PromotionGateDecision", "PromotionGateError",
    "PromotionGatePolicy", "PromotionKind", "evaluate_promotion_gate",
    "validate_policy_table", "wilson_lower_bound",
]
