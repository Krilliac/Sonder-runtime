"""Evaluation suites, result dimensions, and an explicit proposal lifecycle.

The module is an application contract, not an evaluator or persistence adapter.
It gives callers one immutable vocabulary for suite identity, result dimensions,
shadow/canary observations, and promotion evidence.  ``ProposalLifecycle`` is a
small in-memory reference implementation: it refuses invalid transitions and
never promotes a proposal implicitly.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
import hashlib
import json
import math
import re
from types import MappingProxyType
from typing import Any, Mapping, Protocol


SCHEMA = "sonder.evaluation-proposal-lifecycle.v1"
MAX_DIMENSIONS = 32
MAX_METRICS = 64
MAX_PROVENANCE = 64


class EvaluationLifecycleError(ValueError):
    """Raised when an evaluation contract or lifecycle transition is invalid."""


class ProposalState(str, Enum):
    DRAFT = "draft"
    SUBMITTED = "submitted"
    EVALUATING = "evaluating"
    SHADOW = "shadow"
    CANARY = "canary"
    READY_FOR_PROMOTION = "ready_for_promotion"
    PROMOTED = "promoted"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"
    ROLLED_BACK = "rolled_back"


class EvaluationMode(str, Enum):
    OFFLINE = "offline"
    SHADOW = "shadow"
    CANARY = "canary"


def _canonical(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise EvaluationLifecycleError("evaluation values must be JSON-compatible") from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _text(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EvaluationLifecycleError(f"{label} must be a non-empty string")
    return value.strip()


def _metrics(values: Mapping[str, float], label: str) -> Mapping[str, float]:
    if not isinstance(values, Mapping) or not values or len(values) > MAX_METRICS:
        raise EvaluationLifecycleError(f"{label} must contain 1..{MAX_METRICS} metrics")
    clean: dict[str, float] = {}
    for key, value in values.items():
        name = _text(key, f"{label} metric name")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise EvaluationLifecycleError(f"{label}.{name} must be a finite number")
        clean[name] = float(value)
    return MappingProxyType(dict(sorted(clean.items())))


@dataclass(frozen=True)
class EvaluationDimension:
    """A named, deterministic slice of an evaluation suite or result."""

    name: str
    value: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _text(self.name, "dimension name"))
        object.__setattr__(self, "value", _text(self.value, "dimension value"))

    def as_dict(self) -> dict[str, str]:
        return {"name": self.name, "value": self.value}


def _dimensions(values: tuple[EvaluationDimension, ...], label: str) -> tuple[EvaluationDimension, ...]:
    if not isinstance(values, tuple) or not values or len(values) > MAX_DIMENSIONS:
        raise EvaluationLifecycleError(f"{label} must contain 1..{MAX_DIMENSIONS} dimensions")
    if any(not isinstance(item, EvaluationDimension) for item in values):
        raise EvaluationLifecycleError(f"{label} contains an invalid dimension")
    names = tuple(item.name for item in values)
    if names != tuple(sorted(names)) or len(set(names)) != len(names):
        raise EvaluationLifecycleError(f"{label} names must be unique and sorted")
    return values


@dataclass(frozen=True)
class EvaluationSuite:
    """Versioned suite identity and its required result dimensions."""

    suite_id: str
    version: str
    dimensions: tuple[EvaluationDimension, ...]
    metric_names: tuple[str, ...]
    schema: str = SCHEMA

    def __post_init__(self) -> None:
        object.__setattr__(self, "suite_id", _text(self.suite_id, "suite_id"))
        object.__setattr__(self, "version", _text(self.version, "suite version"))
        if self.schema != SCHEMA:
            raise EvaluationLifecycleError(f"unsupported evaluation schema: {self.schema}")
        object.__setattr__(self, "dimensions", _dimensions(self.dimensions, "suite dimensions"))
        names = tuple(_text(item, "metric name") for item in self.metric_names)
        if not names or len(names) > MAX_METRICS or len(set(names)) != len(names):
            raise EvaluationLifecycleError("suite metric_names must be unique and bounded")
        if names != tuple(sorted(names)):
            raise EvaluationLifecycleError("suite metric_names must be sorted")
        object.__setattr__(self, "metric_names", names)

    @property
    def digest(self) -> str:
        return _digest(self.as_dict(include_digest=False))

    def as_dict(self, *, include_digest: bool = True) -> dict[str, Any]:
        result: dict[str, Any] = {
            "schema": self.schema,
            "suite_id": self.suite_id,
            "version": self.version,
            "dimensions": [item.as_dict() for item in self.dimensions],
            "metric_names": list(self.metric_names),
        }
        if include_digest:
            result["suite_digest"] = self.digest
        return result


@dataclass(frozen=True)
class EvaluationResult:
    """One bounded result with suite and dimension identity attached."""

    result_id: str
    suite: EvaluationSuite
    candidate: str
    baseline: str
    mode: EvaluationMode
    dimensions: tuple[EvaluationDimension, ...]
    metrics: Mapping[str, float]
    passed: bool
    sample_count: int
    trajectory_digest: str = ""
    replay_equivalent: bool | None = None
    provenance: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "result_id", _text(self.result_id, "result_id"))
        if not isinstance(self.suite, EvaluationSuite):
            raise EvaluationLifecycleError("result suite is required")
        object.__setattr__(self, "candidate", _text(self.candidate, "candidate"))
        object.__setattr__(self, "baseline", _text(self.baseline, "baseline"))
        if not isinstance(self.mode, EvaluationMode):
            raise EvaluationLifecycleError("result mode is invalid")
        dims = _dimensions(self.dimensions, "result dimensions")
        if dims != self.suite.dimensions:
            raise EvaluationLifecycleError("result dimensions must exactly match its suite")
        object.__setattr__(self, "dimensions", dims)
        metrics = _metrics(self.metrics, "result metrics")
        if tuple(metrics) != self.suite.metric_names:
            raise EvaluationLifecycleError("result metrics must exactly match its suite")
        object.__setattr__(self, "metrics", metrics)
        if not isinstance(self.passed, bool) or isinstance(self.sample_count, bool) or not isinstance(self.sample_count, int) or self.sample_count <= 0:
            raise EvaluationLifecycleError("passed must be boolean and sample_count must be positive")
        if self.trajectory_digest and not isinstance(self.trajectory_digest, str):
            raise EvaluationLifecycleError("trajectory_digest must be a string")
        if self.replay_equivalent is not None and not isinstance(self.replay_equivalent, bool):
            raise EvaluationLifecycleError("replay_equivalent must be boolean or None")
        if not isinstance(self.provenance, tuple) or len(self.provenance) > MAX_PROVENANCE or any(
            not isinstance(item, str) or not item.strip() for item in self.provenance
        ):
            raise EvaluationLifecycleError("provenance must be a bounded tuple of strings")

    @property
    def digest(self) -> str:
        return _digest(self.as_dict(include_digest=False))

    def as_dict(self, *, include_digest: bool = True) -> dict[str, Any]:
        result: dict[str, Any] = {
            "result_id": self.result_id,
            "suite_digest": self.suite.digest,
            "candidate": self.candidate,
            "baseline": self.baseline,
            "mode": self.mode.value,
            "dimensions": [item.as_dict() for item in self.dimensions],
            "metrics": dict(self.metrics),
            "passed": self.passed,
            "sample_count": self.sample_count,
            "trajectory_digest": self.trajectory_digest,
            "replay_equivalent": self.replay_equivalent,
            "provenance": list(self.provenance),
        }
        if include_digest:
            result["result_digest"] = self.digest
        return result


@dataclass(frozen=True)
class ShadowCanaryObservation:
    """Health and bounded measurements for a shadow or canary phase."""

    mode: EvaluationMode
    observation_id: str
    healthy: bool
    sample_count: int
    metrics: Mapping[str, float]
    traffic_fraction: float
    result_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.mode not in (EvaluationMode.SHADOW, EvaluationMode.CANARY):
            raise EvaluationLifecycleError("observation mode must be shadow or canary")
        object.__setattr__(self, "observation_id", _text(self.observation_id, "observation_id"))
        if not isinstance(self.healthy, bool) or isinstance(self.sample_count, bool) or not isinstance(self.sample_count, int) or self.sample_count <= 0:
            raise EvaluationLifecycleError("observation health and sample_count are invalid")
        object.__setattr__(self, "metrics", _metrics(self.metrics, "observation metrics"))
        if isinstance(self.traffic_fraction, bool) or not isinstance(self.traffic_fraction, (int, float)) or not 0 <= self.traffic_fraction <= 1:
            raise EvaluationLifecycleError("traffic_fraction must be between 0 and 1")
        if self.mode is EvaluationMode.SHADOW and self.traffic_fraction != 0:
            raise EvaluationLifecycleError("shadow traffic_fraction must be zero")
        if self.mode is EvaluationMode.CANARY and self.traffic_fraction <= 0:
            raise EvaluationLifecycleError("canary traffic_fraction must be positive")
        if not isinstance(self.result_ids, tuple) or any(not isinstance(item, str) or not item.strip() for item in self.result_ids):
            raise EvaluationLifecycleError("result_ids must be a tuple of strings")


@dataclass(frozen=True)
class PromotionEvidence:
    """Immutable evidence bundle; acceptance never mutates proposal state."""

    proposal_id: str
    candidate: str
    baseline: str
    suite_digest: str
    result_ids: tuple[str, ...]
    dimensions: tuple[EvaluationDimension, ...]
    gate_results: Mapping[str, bool]
    replay_equivalent: bool
    holdout_passed: bool
    rollback_reference: str
    provenance: tuple[str, ...]
    shadow: ShadowCanaryObservation | None = None
    canary: ShadowCanaryObservation | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "proposal_id", _text(self.proposal_id, "proposal_id"))
        object.__setattr__(self, "candidate", _text(self.candidate, "candidate"))
        object.__setattr__(self, "baseline", _text(self.baseline, "baseline"))
        object.__setattr__(self, "suite_digest", _text(self.suite_digest, "suite_digest"))
        if not isinstance(self.result_ids, tuple) or not self.result_ids or any(not isinstance(item, str) or not item.strip() for item in self.result_ids):
            raise EvaluationLifecycleError("promotion evidence needs result IDs")
        object.__setattr__(self, "dimensions", _dimensions(self.dimensions, "evidence dimensions"))
        if not isinstance(self.gate_results, Mapping) or not self.gate_results or any(
            not isinstance(key, str) or not key.strip() or not isinstance(value, bool)
            for key, value in self.gate_results.items()
        ):
            raise EvaluationLifecycleError("gate_results must be a non-empty boolean mapping")
        object.__setattr__(self, "gate_results", MappingProxyType(dict(sorted(self.gate_results.items()))))
        if not isinstance(self.replay_equivalent, bool) or not isinstance(self.holdout_passed, bool):
            raise EvaluationLifecycleError("replay and holdout outcomes must be boolean")
        object.__setattr__(self, "rollback_reference", _text(self.rollback_reference, "rollback_reference"))
        if not isinstance(self.provenance, tuple) or not self.provenance or len(self.provenance) > MAX_PROVENANCE:
            raise EvaluationLifecycleError("promotion provenance is required and bounded")
        if any(not isinstance(item, str) or not item.strip() for item in self.provenance):
            raise EvaluationLifecycleError("promotion provenance contains an invalid item")
        for observation, mode in ((self.shadow, EvaluationMode.SHADOW), (self.canary, EvaluationMode.CANARY)):
            if observation is not None and observation.mode is not mode:
                raise EvaluationLifecycleError(f"{mode.value} observation has the wrong mode")

    @property
    def accepted(self) -> bool:
        return (
            all(self.gate_results.values())
            and self.replay_equivalent
            and self.holdout_passed
            and self.shadow is not None
            and self.shadow.healthy
            and self.canary is not None
            and self.canary.healthy
        )

    @property
    def digest(self) -> str:
        payload = {
            "schema": SCHEMA,
            "proposal_id": self.proposal_id,
            "candidate": self.candidate,
            "baseline": self.baseline,
            "suite_digest": self.suite_digest,
            "result_ids": list(self.result_ids),
            "dimensions": [item.as_dict() for item in self.dimensions],
            "gate_results": dict(self.gate_results),
            "replay_equivalent": self.replay_equivalent,
            "holdout_passed": self.holdout_passed,
            "rollback_reference": self.rollback_reference,
            "provenance": list(self.provenance),
            "shadow": self.shadow.observation_id if self.shadow else None,
            "canary": self.canary.observation_id if self.canary else None,
        }
        return _digest(payload)


@dataclass(frozen=True)
class Proposal:
    proposal_id: str
    candidate: str
    baseline: str
    suite: EvaluationSuite
    state: ProposalState = ProposalState.DRAFT
    result_ids: tuple[str, ...] = ()
    evidence_digest: str = ""
    promotion_kind: str = ""


class GateDecisionLike(Protocol):
    """The parts of a promotion-gate decision the lifecycle relies on."""

    @property
    def gate_results(self) -> Mapping[str, bool]: ...

    @property
    def replay_equivalent(self) -> bool: ...

    @property
    def digest(self) -> str: ...

    @property
    def kind_value(self) -> str: ...


class PromotionGate(Protocol):
    """Mechanical gate fixed when the lifecycle is constructed."""

    def __call__(
        self,
        kind: str,
        *,
        results: tuple[EvaluationResult, ...],
        baseline_pass_rate: float | None,
        case_regressions: int,
        shadow: ShadowCanaryObservation | None,
        canary: ShadowCanaryObservation | None,
    ) -> GateDecisionLike: ...


class ProposalLifecycle:
    """Explicit, non-persistent proposal state machine for evaluation callers.

    Gate authority lives here, not in any service wrapping it.  A proposal
    created with a ``promotion_kind`` is *kind-bound*: its evidence can only be
    built by :meth:`build_gated_promotion_evidence`, which runs the
    ``promotion_gate`` fixed at construction over the results and observations
    this lifecycle recorded, and :meth:`approve` accepts only that gated
    evidence.  Proposals without a kind are legacy; their caller-asserted
    evidence is approved only with an explicit ``allow_ungated_legacy=True``.
    """

    _allowed = {
        ProposalState.DRAFT: {ProposalState.SUBMITTED, ProposalState.WITHDRAWN},
        ProposalState.SUBMITTED: {ProposalState.EVALUATING, ProposalState.REJECTED, ProposalState.WITHDRAWN},
        ProposalState.EVALUATING: {ProposalState.SHADOW, ProposalState.REJECTED, ProposalState.WITHDRAWN},
        ProposalState.SHADOW: {ProposalState.CANARY, ProposalState.REJECTED, ProposalState.WITHDRAWN},
        ProposalState.CANARY: {ProposalState.READY_FOR_PROMOTION, ProposalState.REJECTED, ProposalState.WITHDRAWN},
        ProposalState.READY_FOR_PROMOTION: {ProposalState.PROMOTED, ProposalState.REJECTED},
        ProposalState.PROMOTED: {ProposalState.ROLLED_BACK},
        ProposalState.REJECTED: set(),
        ProposalState.WITHDRAWN: set(),
        ProposalState.ROLLED_BACK: set(),
    }

    def __init__(self, *, promotion_gate: PromotionGate | None = None) -> None:
        if promotion_gate is not None and not callable(promotion_gate):
            raise EvaluationLifecycleError("promotion_gate must be callable")
        self._gate = promotion_gate
        self._gated: dict[str, tuple[str, str]] = {}
        self._proposals: dict[str, Proposal] = {}
        self._results: dict[str, EvaluationResult] = {}
        self._observations: dict[str, dict[EvaluationMode, ShadowCanaryObservation]] = {}
        self._evidence: dict[str, PromotionEvidence] = {}
        self._reproduced_failures: dict[str, dict[str, str]] = {}

    def create(
        self, proposal_id: str, candidate: str, baseline: str, suite: EvaluationSuite,
        *, promotion_kind: str = "",
    ) -> Proposal:
        proposal_id = _text(proposal_id, "proposal_id")
        if proposal_id in self._proposals:
            raise EvaluationLifecycleError(f"proposal {proposal_id!r} already exists")
        if not isinstance(promotion_kind, str):
            raise EvaluationLifecycleError("promotion_kind must be a string")
        if promotion_kind and self._gate is None:
            raise EvaluationLifecycleError("kind-bound proposals require a lifecycle constructed with a promotion_gate")
        proposal = Proposal(
            proposal_id, _text(candidate, "candidate"), _text(baseline, "baseline"), suite,
            promotion_kind=promotion_kind.strip(),
        )
        self._proposals[proposal_id] = proposal
        self._observations[proposal_id] = {}
        self._reproduced_failures[proposal_id] = {}
        return proposal

    def get(self, proposal_id: str) -> Proposal:
        try:
            return self._proposals[proposal_id]
        except KeyError as exc:
            raise EvaluationLifecycleError(f"unknown proposal {proposal_id!r}") from exc

    def recorded_results(self, proposal_id: str) -> tuple[EvaluationResult, ...]:
        """Results accepted for this proposal, in recording order."""
        return tuple(self._results[result_id] for result_id in self.get(proposal_id).result_ids)

    def recorded_observation(self, proposal_id: str, mode: EvaluationMode) -> ShadowCanaryObservation | None:
        """The accepted shadow or canary observation for this proposal, if any."""
        self.get(proposal_id)
        return self._observations[proposal_id].get(mode)

    def record_reproduced_failure(
        self, proposal_id: str, failure_digest: str, source_digest: str,
    ) -> bool:
        """Bind a retained replay divergence to this immutable candidate.

        A changed candidate needs a new proposal. Reattaching the same
        failure is idempotent; fresh failures revoke previously built gates.
        """
        proposal = self.get(proposal_id)
        if proposal.state not in {
            ProposalState.EVALUATING, ProposalState.SHADOW, ProposalState.CANARY,
            ProposalState.READY_FOR_PROMOTION,
        }:
            raise EvaluationLifecycleError("replay failures require an active evaluation or approval")
        if not all(
            isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest)
            for digest in (failure_digest, source_digest)
        ):
            raise EvaluationLifecycleError("replay failure and source digests must be SHA-256 values")
        failures = self._reproduced_failures[proposal_id]
        if failure_digest in failures:
            if failures[failure_digest] != source_digest:
                raise EvaluationLifecycleError("retained replay failure identity changed")
            return False
        failures[failure_digest] = source_digest
        self._evidence.pop(proposal_id, None)
        self._gated.pop(proposal_id, None)
        if proposal.state is ProposalState.READY_FOR_PROMOTION:
            self._proposals[proposal_id] = replace(proposal, state=ProposalState.REJECTED, evidence_digest="")
        return True

    def _transition(self, proposal_id: str, target: ProposalState) -> Proposal:
        current = self.get(proposal_id)
        if target not in self._allowed[current.state]:
            raise EvaluationLifecycleError(f"cannot transition {current.state.value} to {target.value}")
        updated = replace(current, state=target)
        self._proposals[proposal_id] = updated
        return updated

    def submit(self, proposal_id: str) -> Proposal:
        return self._transition(proposal_id, ProposalState.SUBMITTED)

    def begin_evaluation(self, proposal_id: str) -> Proposal:
        return self._transition(proposal_id, ProposalState.EVALUATING)

    def begin_shadow(self, proposal_id: str) -> Proposal:
        return self._transition(proposal_id, ProposalState.SHADOW)

    def begin_canary(self, proposal_id: str) -> Proposal:
        self.get(proposal_id)
        observation = self._observations[proposal_id].get(EvaluationMode.SHADOW)
        if observation is None or not observation.healthy:
            raise EvaluationLifecycleError("a healthy shadow observation is required before canary")
        return self._transition(proposal_id, ProposalState.CANARY)

    def record_result(self, proposal_id: str, result: EvaluationResult) -> EvaluationResult:
        proposal = self.get(proposal_id)
        if proposal.state not in {ProposalState.EVALUATING, ProposalState.SHADOW, ProposalState.CANARY}:
            raise EvaluationLifecycleError("results may only be recorded during evaluation")
        expected_mode = {
            ProposalState.EVALUATING: EvaluationMode.OFFLINE,
            ProposalState.SHADOW: EvaluationMode.SHADOW,
            ProposalState.CANARY: EvaluationMode.CANARY,
        }[proposal.state]
        if result.mode is not expected_mode:
            raise EvaluationLifecycleError(
                f"{proposal.state.value} results require {expected_mode.value} mode"
            )
        if result.suite.digest != proposal.suite.digest or result.candidate != proposal.candidate or result.baseline != proposal.baseline:
            raise EvaluationLifecycleError("result identity does not match proposal")
        existing = self._results.get(result.result_id)
        if existing is not None and existing != result:
            raise EvaluationLifecycleError("result IDs are immutable")
        self._results[result.result_id] = result
        if result.result_id not in proposal.result_ids:
            self._proposals[proposal_id] = replace(proposal, result_ids=proposal.result_ids + (result.result_id,))
            # Evidence binds the exact accepted result set. A late canary
            # result must be evaluated before the proposal can be approved.
            self._evidence.pop(proposal_id, None)
            self._gated.pop(proposal_id, None)
        return result

    def record_observation(self, proposal_id: str, observation: ShadowCanaryObservation) -> ShadowCanaryObservation:
        proposal = self.get(proposal_id)
        expected = ProposalState.SHADOW if observation.mode is EvaluationMode.SHADOW else ProposalState.CANARY
        if proposal.state is not expected:
            raise EvaluationLifecycleError(f"{observation.mode.value} observation requires {expected.value} state")
        if not set(observation.result_ids).issubset(proposal.result_ids):
            raise EvaluationLifecycleError("observation references results not recorded for this proposal")
        previous = self._observations[proposal_id].get(observation.mode)
        if previous is not None and previous != observation:
            raise EvaluationLifecycleError("observation mode already has an immutable record")
        self._observations[proposal_id][observation.mode] = observation
        return observation

    def build_promotion_evidence(
        self,
        proposal_id: str,
        *,
        gate_results: Mapping[str, bool],
        replay_equivalent: bool,
        holdout_passed: bool,
        rollback_reference: str,
        provenance: tuple[str, ...],
    ) -> PromotionEvidence:
        """Legacy, caller-asserted evidence; refused for kind-bound proposals."""
        if self.get(proposal_id).promotion_kind:
            raise EvaluationLifecycleError(
                "kind-bound proposals require gated promotion evidence; caller-asserted gates are refused",
            )
        self._gated.pop(proposal_id, None)
        return self._build_evidence(
            proposal_id, gate_results=gate_results, replay_equivalent=replay_equivalent,
            holdout_passed=holdout_passed, rollback_reference=rollback_reference, provenance=provenance,
        )

    def promotion_gate_decision(
        self, proposal_id: str, *, baseline_pass_rate: float | None, case_regressions: int,
    ) -> GateDecisionLike:
        """Run the construction-time gate over this lifecycle's recorded evidence."""
        proposal = self.get(proposal_id)
        if not proposal.promotion_kind or self._gate is None:
            raise EvaluationLifecycleError(f"proposal {proposal_id!r} has no promotion kind to gate")
        if type(case_regressions) is not int or case_regressions < 0:
            raise EvaluationLifecycleError("case_regressions must be a non-negative integer")
        decision = self._gate(
            proposal.promotion_kind,
            results=self.recorded_results(proposal_id),
            baseline_pass_rate=baseline_pass_rate,
            case_regressions=case_regressions + len(self._reproduced_failures[proposal_id]),
            shadow=self._observations[proposal_id].get(EvaluationMode.SHADOW),
            canary=self._observations[proposal_id].get(EvaluationMode.CANARY),
        )
        if decision.kind_value != proposal.promotion_kind:
            raise EvaluationLifecycleError("promotion gate returned a decision for a different kind")
        return decision

    def build_gated_promotion_evidence(
        self,
        proposal_id: str,
        *,
        baseline_pass_rate: float | None,
        case_regressions: int,
        holdout_passed: bool,
        rollback_reference: str,
        provenance: tuple[str, ...],
    ) -> tuple[PromotionEvidence, GateDecisionLike]:
        """Build evidence whose gates are this lifecycle's own recomputed decision."""
        decision = self.promotion_gate_decision(
            proposal_id, baseline_pass_rate=baseline_pass_rate, case_regressions=case_regressions,
        )
        evidence = self._build_evidence(
            proposal_id,
            gate_results=decision.gate_results,
            replay_equivalent=decision.replay_equivalent,
            holdout_passed=holdout_passed,
            rollback_reference=rollback_reference,
            provenance=tuple(provenance) + (f"promotion-gate:{decision.kind_value}:{decision.digest}",),
        )
        self._gated[proposal_id] = (evidence.digest, decision.digest)
        return evidence, decision

    def gated_evidence(self, proposal_id: str) -> tuple[str, str] | None:
        """(evidence digest, gate decision digest) of the latest gated evidence."""
        self.get(proposal_id)
        return self._gated.get(proposal_id)

    def _build_evidence(
        self,
        proposal_id: str,
        *,
        gate_results: Mapping[str, bool],
        replay_equivalent: bool,
        holdout_passed: bool,
        rollback_reference: str,
        provenance: tuple[str, ...],
    ) -> PromotionEvidence:
        proposal = self.get(proposal_id)
        if proposal.state is not ProposalState.CANARY:
            raise EvaluationLifecycleError("promotion evidence requires canary state")
        results = tuple(self._results[result_id] for result_id in proposal.result_ids)
        if not results:
            raise EvaluationLifecycleError("promotion evidence requires at least one result")
        evidence = PromotionEvidence(
            proposal_id, proposal.candidate, proposal.baseline, proposal.suite.digest,
            proposal.result_ids, proposal.suite.dimensions, gate_results, replay_equivalent,
            holdout_passed, rollback_reference, provenance,
            self._observations[proposal_id].get(EvaluationMode.SHADOW),
            self._observations[proposal_id].get(EvaluationMode.CANARY),
        )
        self._evidence[proposal_id] = evidence
        return evidence

    def approve(self, proposal_id: str, evidence_digest: str, *, allow_ungated_legacy: bool = False) -> Proposal:
        proposal = self.get(proposal_id)
        gated = self._gated.get(proposal_id)
        if proposal.promotion_kind:
            if allow_ungated_legacy:
                raise EvaluationLifecycleError("kind-bound proposals cannot use the ungated legacy opt-in")
            if gated is None or gated[0] != evidence_digest:
                raise EvaluationLifecycleError("kind-bound proposals can only be approved with gated promotion evidence")
        elif not allow_ungated_legacy:
            raise EvaluationLifecycleError(
                "ungated approval of a proposal without a promotion kind requires allow_ungated_legacy=True",
            )
        elif self._reproduced_failures[proposal_id]:
            raise EvaluationLifecycleError("legacy approval cannot ignore a retained replay divergence")
        evidence = self._evidence.get(proposal_id)
        if (evidence is None or evidence.digest != evidence_digest
            or evidence.result_ids != proposal.result_ids or not evidence.accepted):
            raise EvaluationLifecycleError("promotion evidence is absent, stale, or rejected")
        updated = self._transition(proposal_id, ProposalState.READY_FOR_PROMOTION)
        self._proposals[proposal_id] = replace(updated, evidence_digest=evidence.digest)
        return self._proposals[proposal_id]

    def promote(self, proposal_id: str, evidence_digest: str, *, attended: bool = False) -> Proposal:
        if not attended:
            raise EvaluationLifecycleError("promotion requires an attended explicit decision")
        proposal = self.get(proposal_id)
        if proposal.state is not ProposalState.READY_FOR_PROMOTION or proposal.evidence_digest != evidence_digest:
            raise EvaluationLifecycleError("proposal is not approved with the supplied evidence")
        return self._transition(proposal_id, ProposalState.PROMOTED)

    def reject(self, proposal_id: str) -> Proposal:
        return self._transition(proposal_id, ProposalState.REJECTED)

    def withdraw(self, proposal_id: str) -> Proposal:
        return self._transition(proposal_id, ProposalState.WITHDRAWN)

    def rollback(self, proposal_id: str, *, attended: bool = False) -> Proposal:
        if not attended:
            raise EvaluationLifecycleError("proposal rollback requires an attended decision")
        return self._transition(proposal_id, ProposalState.ROLLED_BACK)


__all__ = [
    "EvaluationDimension", "EvaluationLifecycleError", "EvaluationMode", "EvaluationResult",
    "EvaluationSuite", "GateDecisionLike", "Proposal", "ProposalLifecycle", "PromotionGate", "ProposalState",
    "PromotionEvidence", "ShadowCanaryObservation",
]
