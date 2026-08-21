"""Provider-neutral ports for the live evaluation application boundary."""
from __future__ import annotations

from typing import Callable, Mapping, Protocol, Sequence

from ..evaluation.corpus_inventory import CorpusSourceReport
from ..evaluation.proposal_lifecycle import (
    EvaluationResult,
    EvaluationSuite,
    Proposal,
    PromotionEvidence,
    ShadowCanaryObservation,
)
from ..evaluation.trajectory_replay import ReplayReport, TrajectoryRecord


class EvaluationCorpusPort(Protocol):
    """Supply bounded source reports; adapters own how sources are read."""

    def scan(self) -> Sequence[CorpusSourceReport]: ...


class EvaluationSuiteCatalog(Protocol):
    """Resolve and retain immutable suite identities for a live composition."""

    def register(self, suite: EvaluationSuite) -> EvaluationSuite: ...

    def resolve(self, suite_id: str, version: str) -> EvaluationSuite | None: ...


class EvaluationLifecyclePort(Protocol):
    """Proposal lifecycle operations exposed to application callers."""

    def create(self, proposal_id: str, candidate: str, baseline: str, suite: EvaluationSuite) -> Proposal: ...
    def submit(self, proposal_id: str) -> Proposal: ...
    def begin_evaluation(self, proposal_id: str) -> Proposal: ...
    def begin_shadow(self, proposal_id: str) -> Proposal: ...
    def begin_canary(self, proposal_id: str) -> Proposal: ...
    def record_result(self, proposal_id: str, result: EvaluationResult) -> EvaluationResult: ...
    def record_observation(self, proposal_id: str, observation: ShadowCanaryObservation) -> ShadowCanaryObservation: ...
    def build_promotion_evidence(
        self,
        proposal_id: str,
        *,
        gate_results: Mapping[str, bool],
        replay_equivalent: bool,
        holdout_passed: bool,
        rollback_reference: str,
        provenance: tuple[str, ...],
    ) -> PromotionEvidence: ...
    def approve(self, proposal_id: str, evidence_digest: str) -> Proposal: ...
    def promote(self, proposal_id: str, evidence_digest: str, *, attended: bool = False) -> Proposal: ...
    def reject(self, proposal_id: str) -> Proposal: ...
    def withdraw(self, proposal_id: str) -> Proposal: ...
    def rollback(self, proposal_id: str, *, attended: bool = False) -> Proposal: ...


TrajectoryEvaluator = Callable[[object], object]


class TrajectoryReplayPort(Protocol):
    """Replay a trajectory through a provider supplied by the caller."""

    def replay(self, expected: TrajectoryRecord, evaluator: TrajectoryEvaluator) -> ReplayReport: ...


__all__ = [
    "EvaluationCorpusPort",
    "EvaluationLifecyclePort",
    "EvaluationSuiteCatalog",
    "TrajectoryEvaluator",
    "TrajectoryReplayPort",
]
