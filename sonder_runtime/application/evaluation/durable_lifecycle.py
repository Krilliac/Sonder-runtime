"""Durable application boundary for evaluation proposal lifecycles.

``ProposalLifecycle`` remains the fail-closed state machine.  This service
adds the missing persistence seam: every accepted mutation is recorded as an
immutable event through the repository port, including the exact promotion
evidence digest and rollback reference.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Protocol

from ..ports.session_repository import SessionEvent
from .proposal_lifecycle import (
    EvaluationResult,
    EvaluationSuite,
    Proposal,
    ProposalLifecycle,
    PromotionEvidence,
    ShadowCanaryObservation,
)


class EvaluationLifecycleRepository(Protocol):
    def append(self, proposal_id: str, event_type: str, payload: Mapping[str, object]) -> SessionEvent: ...

    def history(self, proposal_id: str, *, limit: int = 1_000) -> tuple[SessionEvent, ...]: ...


@dataclass(frozen=True)
class EvaluationLifecycleService:
    """Persist successful lifecycle mutations after domain validation."""

    lifecycle: ProposalLifecycle
    repository: EvaluationLifecycleRepository

    def _record(self, proposal: Proposal, event_type: str, **payload: object) -> None:
        body = {"proposal_id": proposal.proposal_id, "state": proposal.state.value, **payload}
        self.repository.append(proposal.proposal_id, event_type, body)

    def create(self, proposal_id: str, candidate: str, baseline: str, suite: EvaluationSuite) -> Proposal:
        proposal = self.lifecycle.create(proposal_id, candidate, baseline, suite)
        self._record(proposal, "evaluation.proposal.created", candidate=proposal.candidate,
                     baseline=proposal.baseline, suite_digest=suite.digest)
        return proposal

    def submit(self, proposal_id: str) -> Proposal:
        proposal = self.lifecycle.submit(proposal_id)
        self._record(proposal, "evaluation.proposal.submitted")
        return proposal

    def begin_evaluation(self, proposal_id: str) -> Proposal:
        proposal = self.lifecycle.begin_evaluation(proposal_id)
        self._record(proposal, "evaluation.proposal.evaluating")
        return proposal

    def begin_shadow(self, proposal_id: str) -> Proposal:
        proposal = self.lifecycle.begin_shadow(proposal_id)
        self._record(proposal, "evaluation.proposal.shadow")
        return proposal

    def begin_canary(self, proposal_id: str) -> Proposal:
        proposal = self.lifecycle.begin_canary(proposal_id)
        self._record(proposal, "evaluation.proposal.canary")
        return proposal

    def record_result(self, proposal_id: str, result: EvaluationResult) -> EvaluationResult:
        result = self.lifecycle.record_result(proposal_id, result)
        self._record(self.lifecycle.get(proposal_id), "evaluation.result.recorded",
                     result_id=result.result_id, result_digest=result.digest, mode=result.mode.value)
        return result

    def record_observation(self, proposal_id: str, observation: ShadowCanaryObservation) -> ShadowCanaryObservation:
        observation = self.lifecycle.record_observation(proposal_id, observation)
        self._record(self.lifecycle.get(proposal_id), "evaluation.observation.recorded",
                     observation_id=observation.observation_id, mode=observation.mode.value,
                     healthy=observation.healthy)
        return observation

    def build_promotion_evidence(self, proposal_id: str, **kwargs: object) -> PromotionEvidence:
        evidence = self.lifecycle.build_promotion_evidence(proposal_id, **kwargs)
        self._record(self.lifecycle.get(proposal_id), "evaluation.evidence.attached",
                     evidence_digest=evidence.digest, evidence=_evidence_payload(evidence))
        return evidence

    def approve(self, proposal_id: str, evidence_digest: str) -> Proposal:
        proposal = self.lifecycle.approve(proposal_id, evidence_digest)
        self._record(proposal, "evaluation.proposal.approved", evidence_digest=evidence_digest)
        return proposal

    def promote(self, proposal_id: str, evidence_digest: str, *, attended: bool = False) -> Proposal:
        proposal = self.lifecycle.promote(proposal_id, evidence_digest, attended=attended)
        self._record(proposal, "evaluation.proposal.promoted", evidence_digest=evidence_digest, attended=attended)
        return proposal

    def reject(self, proposal_id: str) -> Proposal:
        proposal = self.lifecycle.reject(proposal_id)
        self._record(proposal, "evaluation.proposal.rejected")
        return proposal

    def withdraw(self, proposal_id: str) -> Proposal:
        proposal = self.lifecycle.withdraw(proposal_id)
        self._record(proposal, "evaluation.proposal.withdrawn")
        return proposal

    def rollback(self, proposal_id: str, *, attended: bool = False) -> Proposal:
        proposal = self.lifecycle.rollback(proposal_id, attended=attended)
        self._record(proposal, "evaluation.proposal.rolled_back", attended=attended)
        return proposal

    def history(self, proposal_id: str, *, limit: int = 1_000) -> tuple[SessionEvent, ...]:
        return self.repository.history(proposal_id, limit=limit)


def _evidence_payload(evidence: PromotionEvidence) -> dict[str, object]:
    return {
        "proposal_id": evidence.proposal_id,
        "candidate": evidence.candidate,
        "baseline": evidence.baseline,
        "suite_digest": evidence.suite_digest,
        "result_ids": list(evidence.result_ids),
        "dimensions": [dimension.as_dict() for dimension in evidence.dimensions],
        "gate_results": dict(evidence.gate_results),
        "replay_equivalent": evidence.replay_equivalent,
        "holdout_passed": evidence.holdout_passed,
        "rollback_reference": evidence.rollback_reference,
        "provenance": list(evidence.provenance),
        "shadow_id": evidence.shadow.observation_id if evidence.shadow else None,
        "canary_id": evidence.canary.observation_id if evidence.canary else None,
        "evidence_digest": evidence.digest,
    }


__all__ = ["EvaluationLifecycleRepository", "EvaluationLifecycleService"]
