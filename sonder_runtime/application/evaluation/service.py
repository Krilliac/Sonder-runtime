"""Live, provider-neutral application composition for evaluation.

The service is deliberately an orchestration boundary.  It owns no model,
corpus, deployment, or persistence implementation: those arrive through
ports, while the existing typed evaluation modules enforce the invariants.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Sequence

from ..ports.evaluation import (
    EvaluationCorpusPort,
    EvaluationLifecyclePort,
    EvaluationSuiteCatalog,
    TrajectoryEvaluator,
)
from .corpus_inventory import EvaluationCorpusInventory, build_inventory
from .divergence import (
    DivergencePolicy,
    EvaluatorFactory,
    InMemoryMinimizedFailureStore,
    MeaningfulDivergence,
    MinimizedFailure,
    MinimizedFailureStore,
    minimize_failure,
    replay_divergence,
    reproduce,
)
from .promotion_gates import (
    DEFAULT_PROMOTION_GATE_POLICIES,
    PromotionGateDecision,
    PromotionGatePolicy,
    PromotionKind,
    evaluate_promotion_gate,
    validate_policy_table,
)
from .proposal_lifecycle import (
    EvaluationResult,
    EvaluationSuite,
    Proposal,
    PromotionEvidence,
    ShadowCanaryObservation,
)
from .trajectory_replay import ReplayReport, TrajectoryRecord, replay_trajectory


@dataclass
class InMemoryEvaluationSuiteCatalog:
    """Small reference catalog for live use and composition tests."""

    _suites: dict[tuple[str, str], EvaluationSuite] = field(default_factory=dict)

    def register(self, suite: EvaluationSuite) -> EvaluationSuite:
        key = (suite.suite_id, suite.version)
        existing = self._suites.get(key)
        if existing is not None and existing != suite:
            raise ValueError(f"suite {suite.suite_id!r} version {suite.version!r} is immutable")
        self._suites[key] = suite
        return suite

    def resolve(self, suite_id: str, version: str) -> EvaluationSuite | None:
        return self._suites.get((suite_id, version))


class EvaluationApplicationService:
    """Compose suite, corpus, replay, lifecycle, and evidence operations."""

    def __init__(
        self,
        *,
        corpus: EvaluationCorpusPort,
        lifecycle: EvaluationLifecyclePort,
        suites: EvaluationSuiteCatalog | None = None,
        failures: MinimizedFailureStore | None = None,
        gate_policies: Mapping[PromotionKind, PromotionGatePolicy] | None = None,
    ) -> None:
        self._corpus = corpus
        self._lifecycle = lifecycle
        self._suites = suites or InMemoryEvaluationSuiteCatalog()
        self._failures = failures or InMemoryMinimizedFailureStore()
        self._gate_policies = validate_policy_table(gate_policies or DEFAULT_PROMOTION_GATE_POLICIES)

    def register_suite(self, suite: EvaluationSuite) -> EvaluationSuite:
        return self._suites.register(suite)

    def resolve_suite(self, suite_id: str, version: str) -> EvaluationSuite | None:
        return self._suites.resolve(suite_id, version)

    def inventory(self, *, require_complete: bool = False) -> EvaluationCorpusInventory:
        inventory = build_inventory(tuple(self._corpus.scan()))
        return inventory.require_complete() if require_complete else inventory

    @staticmethod
    def replay(expected: TrajectoryRecord, evaluator: TrajectoryEvaluator) -> ReplayReport:
        return replay_trajectory(expected, evaluator)

    @staticmethod
    def earliest_divergence(
        expected: TrajectoryRecord,
        evaluator_factory: EvaluatorFactory,
        policy: DivergencePolicy | None = None,
    ) -> MeaningfulDivergence | None:
        """Replay through a fresh evaluator and return the first decision divergence."""
        return replay_divergence(expected, evaluator_factory, policy)

    def minimize_and_retain_failure(
        self,
        expected: TrajectoryRecord,
        evaluator_factory: EvaluatorFactory,
        policy: DivergencePolicy | None = None,
        *,
        baseline_factory: EvaluatorFactory | None = None,
        max_evaluations: int = 256,
    ) -> MinimizedFailure:
        """Minimize a divergent replay, prove it reproduces, and retain it."""
        failure = minimize_failure(
            expected, evaluator_factory, policy,
            baseline_factory=baseline_factory, max_evaluations=max_evaluations,
        )
        self._failures.retain(failure)
        return failure

    def retained_failures(self) -> tuple[str, ...]:
        return self._failures.digests()

    def reproduce_retained_failure(
        self, failure_digest: str, evaluator_factory: EvaluatorFactory,
    ) -> MeaningfulDivergence | None:
        """Replay a retained failure; ``None`` means the candidate no longer diverges."""
        return reproduce(self._failures.load(failure_digest), evaluator_factory)

    def gate_policy(self, kind: PromotionKind) -> PromotionGatePolicy:
        return self._gate_policies[kind]

    def evaluate_promotion_gate(
        self,
        kind: PromotionKind,
        *,
        results: Sequence[EvaluationResult],
        baseline_pass_rate: float | None = None,
        case_regressions: int = 0,
        shadow: ShadowCanaryObservation | None = None,
        canary: ShadowCanaryObservation | None = None,
    ) -> PromotionGateDecision:
        return evaluate_promotion_gate(
            self._gate_policies[kind], results=results, baseline_pass_rate=baseline_pass_rate,
            case_regressions=case_regressions, shadow=shadow, canary=canary,
        )

    def gated_promotion_evidence(
        self,
        proposal_id: str,
        decision: PromotionGateDecision,
        *,
        holdout_passed: bool,
        rollback_reference: str,
        provenance: tuple[str, ...],
    ) -> PromotionEvidence:
        """Build promotion evidence whose gates are exactly a mechanical decision.

        The decision's named sub-gates become the evidence ``gate_results`` and
        its digest is appended to provenance, so any failed sub-gate makes the
        evidence unacceptable and ``approve`` refuses it.
        """
        return self.promotion_evidence(
            proposal_id,
            gate_results=decision.gate_results,
            replay_equivalent=decision.replay_equivalent,
            holdout_passed=holdout_passed,
            rollback_reference=rollback_reference,
            provenance=tuple(provenance) + (f"promotion-gate:{decision.kind.value}:{decision.digest}",),
        )

    def create_proposal(self, proposal_id: str, candidate: str, baseline: str, suite: EvaluationSuite) -> Proposal:
        return self._lifecycle.create(proposal_id, candidate, baseline, suite)

    def submit(self, proposal_id: str) -> Proposal:
        return self._lifecycle.submit(proposal_id)

    def begin_evaluation(self, proposal_id: str) -> Proposal:
        return self._lifecycle.begin_evaluation(proposal_id)

    def begin_shadow(self, proposal_id: str) -> Proposal:
        return self._lifecycle.begin_shadow(proposal_id)

    def begin_canary(self, proposal_id: str) -> Proposal:
        return self._lifecycle.begin_canary(proposal_id)

    def record_result(self, proposal_id: str, result: EvaluationResult) -> EvaluationResult:
        return self._lifecycle.record_result(proposal_id, result)

    def record_observation(self, proposal_id: str, observation: ShadowCanaryObservation) -> ShadowCanaryObservation:
        return self._lifecycle.record_observation(proposal_id, observation)

    def promotion_evidence(
        self,
        proposal_id: str,
        *,
        gate_results: Mapping[str, bool],
        replay_equivalent: bool,
        holdout_passed: bool,
        rollback_reference: str,
        provenance: tuple[str, ...],
    ) -> PromotionEvidence:
        return self._lifecycle.build_promotion_evidence(
            proposal_id,
            gate_results=gate_results,
            replay_equivalent=replay_equivalent,
            holdout_passed=holdout_passed,
            rollback_reference=rollback_reference,
            provenance=provenance,
        )

    def approve(self, proposal_id: str, evidence_digest: str) -> Proposal:
        return self._lifecycle.approve(proposal_id, evidence_digest)

    def promote(self, proposal_id: str, evidence_digest: str, *, attended: bool = False) -> Proposal:
        return self._lifecycle.promote(proposal_id, evidence_digest, attended=attended)

    def reject(self, proposal_id: str) -> Proposal:
        return self._lifecycle.reject(proposal_id)

    def withdraw(self, proposal_id: str) -> Proposal:
        return self._lifecycle.withdraw(proposal_id)

    def rollback(self, proposal_id: str, *, attended: bool = False) -> Proposal:
        return self._lifecycle.rollback(proposal_id, attended=attended)


__all__ = ["EvaluationApplicationService", "InMemoryEvaluationSuiteCatalog"]
