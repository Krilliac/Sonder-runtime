"""Live, provider-neutral application composition for evaluation.

The service is deliberately an orchestration boundary.  It owns no model,
corpus, deployment, or persistence implementation: those arrive through
ports, while the existing typed evaluation modules enforce the invariants.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

from ..ports.evaluation import (
    EvaluationCorpusPort,
    EvaluationLifecyclePort,
    EvaluationSuiteCatalog,
    TrajectoryEvaluator,
)
from .corpus_inventory import EvaluationCorpusInventory, build_inventory
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
    ) -> None:
        self._corpus = corpus
        self._lifecycle = lifecycle
        self._suites = suites or InMemoryEvaluationSuiteCatalog()

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
