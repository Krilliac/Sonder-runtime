"""Durable application boundary for evaluation proposal lifecycles.

``ProposalLifecycle`` remains the fail-closed state machine.  This service
adds the missing persistence seam: every accepted mutation is recorded as an
immutable event through the repository port, including the exact promotion
evidence digest and rollback reference.  Gate authority is persisted too: the
creation event carries the proposal's ``promotion_kind``, evidence events say
whether the evidence was ``gated`` and carry the gate decision digest, and the
approval event records whether it was gated or a legacy opt-in.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Protocol

from ..ports.session_repository import SessionEvent
from .proposal_lifecycle import (
    EvaluationMode,
    GateDecisionLike,
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

    def create(
        self, proposal_id: str, candidate: str, baseline: str, suite: EvaluationSuite,
        *, promotion_kind: str = "",
    ) -> Proposal:
        proposal = self.lifecycle.create(proposal_id, candidate, baseline, suite, promotion_kind=promotion_kind)
        self._record(proposal, "evaluation.proposal.created", candidate=proposal.candidate,
                     baseline=proposal.baseline, suite_digest=suite.digest,
                     promotion_kind=proposal.promotion_kind)
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

    def recorded_results(self, proposal_id: str) -> tuple[EvaluationResult, ...]:
        return self.lifecycle.recorded_results(proposal_id)

    def recorded_observation(self, proposal_id: str, mode: EvaluationMode) -> ShadowCanaryObservation | None:
        return self.lifecycle.recorded_observation(proposal_id, mode)

    def record_reproduced_failure(
        self, proposal_id: str, failure_digest: str, source_digest: str,
    ) -> bool:
        added = self.lifecycle.record_reproduced_failure(proposal_id, failure_digest, source_digest)
        if added:
            self._record(self.lifecycle.get(proposal_id), "evaluation.failure.retained",
                         failure_digest=failure_digest, source_digest=source_digest)
        return added

    def build_promotion_evidence(self, proposal_id: str, **kwargs: object) -> PromotionEvidence:
        evidence = self.lifecycle.build_promotion_evidence(proposal_id, **kwargs)
        self._record(self.lifecycle.get(proposal_id), "evaluation.evidence.attached",
                     evidence_digest=evidence.digest, evidence=_evidence_payload(evidence),
                     gated=False, gate_decision_digest=None)
        return evidence

    def promotion_gate_decision(
        self, proposal_id: str, *, baseline_pass_rate: float | None, case_regressions: int,
    ) -> GateDecisionLike:
        return self.lifecycle.promotion_gate_decision(
            proposal_id, baseline_pass_rate=baseline_pass_rate, case_regressions=case_regressions,
        )

    def build_gated_promotion_evidence(
        self, proposal_id: str, **kwargs: object,
    ) -> tuple[PromotionEvidence, GateDecisionLike]:
        evidence, decision = self.lifecycle.build_gated_promotion_evidence(proposal_id, **kwargs)
        self._record(self.lifecycle.get(proposal_id), "evaluation.evidence.attached",
                     evidence_digest=evidence.digest, evidence=_evidence_payload(evidence),
                     gated=True, gate_decision_digest=decision.digest,
                     promotion_kind=decision.kind_value)
        return evidence, decision

    def gated_evidence(self, proposal_id: str) -> tuple[str, str] | None:
        return self.lifecycle.gated_evidence(proposal_id)

    def approve(self, proposal_id: str, evidence_digest: str, *, allow_ungated_legacy: bool = False) -> Proposal:
        proposal = self.lifecycle.approve(proposal_id, evidence_digest, allow_ungated_legacy=allow_ungated_legacy)
        gated = self.lifecycle.gated_evidence(proposal_id)
        self._record(proposal, "evaluation.proposal.approved", evidence_digest=evidence_digest,
                     gated=bool(gated and gated[0] == evidence_digest),
                     gate_decision_digest=gated[1] if gated and gated[0] == evidence_digest else None)
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
