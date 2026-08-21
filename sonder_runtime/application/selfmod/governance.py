"""Evidence-first governance for self-modification candidates.

This module is an application-level policy boundary.  It records what was
actually verified and reviewed before producing a deployment intent; it does
not edit files, invoke git, create worktrees, deploy code, or push remotely.

``unrestricted`` is deliberately represented as a bypass, never as evidence:
missing isolation, verification, and review are reported as bypassed gates.
Even in that mode a deployment intent cannot request an automatic remote push.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum

from ...domain.common.errors import Conflict, Forbidden, NotFound
from .reproducer_contract import (
    BenchmarkEvidence,
    FailureEvidence,
    ReproducerContractService,
    ReproducerEvidence,
)


class GovernancePhase(str, Enum):
    PROPOSED = "proposed"
    ISOLATED = "isolated"
    VERIFIED = "verified"
    REVIEWED = "reviewed"
    APPROVED = "approved"
    DEPLOYMENT_INTENDED = "deployment_intended"
    REJECTED = "rejected"


class GovernanceInputError(ValueError):
    """Raised when a governance record cannot be made auditable."""


@dataclass(frozen=True, slots=True)
class WorktreeMetadata:
    """Identity supplied by an external worktree adapter, not created here."""

    path: str
    branch: str
    commit: str
    isolated: bool = True
    clean: bool = True
    managed: bool = True

    def __post_init__(self) -> None:
        for name in ("path", "branch", "commit"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise GovernanceInputError(f"{name} is required")
        if not all(isinstance(value, bool) for value in (self.isolated, self.clean, self.managed)):
            raise GovernanceInputError("worktree flags must be boolean")


@dataclass(frozen=True, slots=True)
class VerificationEvidence:
    """One independently produced verification result."""

    evidence_id: str
    check: str
    passed: bool
    artifact_digest: str
    summary: str = ""

    def __post_init__(self) -> None:
        for name in ("evidence_id", "check", "artifact_digest"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise GovernanceInputError(f"{name} is required")
        if not isinstance(self.passed, bool):
            raise GovernanceInputError("verification passed must be boolean")


@dataclass(frozen=True, slots=True)
class ReviewEvidence:
    """Human or independent-agent review, distinct from test evidence."""

    review_id: str
    reviewer: str
    approved: bool
    evidence_ids: tuple[str, ...]
    summary: str = ""

    def __post_init__(self) -> None:
        for name in ("review_id", "reviewer"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise GovernanceInputError(f"{name} is required")
        if not isinstance(self.approved, bool):
            raise GovernanceInputError("review approved must be boolean")
        if not self.evidence_ids or any(not isinstance(item, str) or not item.strip() for item in self.evidence_ids):
            raise GovernanceInputError("review must cite evidence ids")


@dataclass(frozen=True, slots=True)
class CandidateRecord:
    candidate_id: str
    objective: str
    baseline_digest: str
    phase: GovernancePhase = GovernancePhase.PROPOSED
    worktree: WorktreeMetadata | None = None
    verifications: tuple[VerificationEvidence, ...] = ()
    review: ReviewEvidence | None = None
    unrestricted: bool = False
    bypassed_gates: tuple[str, ...] = ()
    rejection_reason: str = ""
    reproducer_evidence: tuple[ReproducerEvidence, ...] = ()

    def __post_init__(self) -> None:
        for name in ("candidate_id", "objective", "baseline_digest"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise GovernanceInputError(f"{name} is required")
        if not isinstance(self.unrestricted, bool):
            raise GovernanceInputError("unrestricted must be boolean")


@dataclass(frozen=True, slots=True)
class DeploymentIntent:
    """A local deployment authorization; execution and pushing are separate."""

    candidate_id: str
    allowed: bool
    reason: str
    bypassed_gates: tuple[str, ...] = ()
    automatic_push: bool = False
    remote_push_allowed: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.candidate_id, str) or not self.candidate_id.strip():
            raise GovernanceInputError("candidate_id is required")
        if self.automatic_push or self.remote_push_allowed:
            raise GovernanceInputError("governance never authorizes automatic remote push")


class SelfmodGovernance:
    """Stateful, persistence-neutral governance for candidate records."""

    def __init__(self) -> None:
        self._candidates: dict[str, CandidateRecord] = {}

    def propose(self, candidate_id: str, objective: str, baseline_digest: str, *, unrestricted: bool = False) -> CandidateRecord:
        if candidate_id in self._candidates:
            raise Conflict(f"candidate {candidate_id!r} already exists")
        record = CandidateRecord(candidate_id, objective, baseline_digest, unrestricted=unrestricted)
        self._candidates[candidate_id] = record
        return record

    def attach_worktree(self, candidate_id: str, metadata: WorktreeMetadata) -> CandidateRecord:
        record = self._get(candidate_id)
        self._require_phase(record, GovernancePhase.PROPOSED)
        if (not metadata.isolated or not metadata.managed) and not record.unrestricted:
            return self._reject(record, "isolated_managed_worktree_required")
        bypasses = record.bypassed_gates
        if not metadata.isolated:
            bypasses = _append_once(bypasses, "isolation")
        if not metadata.managed:
            bypasses = _append_once(bypasses, "worktree_management")
        updated = replace(record, phase=GovernancePhase.ISOLATED, worktree=metadata, bypassed_gates=bypasses)
        self._candidates[candidate_id] = updated
        return updated

    def record_verification(self, candidate_id: str, evidence: VerificationEvidence) -> CandidateRecord:
        record = self._get(candidate_id)
        # A candidate may accumulate multiple independent checks before
        # review.  The first check moves it to VERIFIED for compatibility;
        # subsequent checks remain append-only in that same pre-review phase.
        self._require_phase(record, GovernancePhase.ISOLATED, GovernancePhase.VERIFIED)
        if not evidence.passed and not record.unrestricted:
            return self._reject(record, f"verification_failed:{evidence.evidence_id}")
        updated = replace(record, phase=GovernancePhase.VERIFIED,
                          verifications=record.verifications + (evidence,))
        if not evidence.passed:
            updated = replace(updated, bypassed_gates=_append_once(record.bypassed_gates, "verification"))
        self._candidates[candidate_id] = updated
        return updated

    def record_reproducer(self, candidate_id: str, evidence: ReproducerEvidence) -> CandidateRecord:
        """Attach one concrete failure or benchmark contract to a candidate."""
        record = self._get(candidate_id)
        self._require_phase(record, GovernancePhase.ISOLATED)
        if isinstance(evidence, FailureEvidence):
            ReproducerContractService().validate_failure(evidence)
        elif isinstance(evidence, BenchmarkEvidence):
            ReproducerContractService().validate_benchmark(evidence)
        else:
            raise GovernanceInputError(
                "reproducer evidence must be FailureEvidence or BenchmarkEvidence"
            )
        if any(item.evidence_id == evidence.evidence_id for item in record.reproducer_evidence):
            raise Conflict(f"reproducer evidence {evidence.evidence_id!r} already exists")
        updated = replace(
            record,
            reproducer_evidence=record.reproducer_evidence + (evidence,),
        )
        self._candidates[candidate_id] = updated
        return updated

    def record_review(self, candidate_id: str, review: ReviewEvidence) -> CandidateRecord:
        record = self._get(candidate_id)
        self._require_phase(record, GovernancePhase.VERIFIED)
        known = {item.evidence_id for item in record.verifications}
        if not set(review.evidence_ids).issubset(known):
            raise GovernanceInputError("review cites unknown verification evidence")
        if not review.approved and not record.unrestricted:
            return self._reject(record, "review_rejected")
        bypasses = record.bypassed_gates
        if not review.approved:
            bypasses = _append_once(bypasses, "review")
        updated = replace(record, phase=GovernancePhase.REVIEWED, review=review, bypassed_gates=bypasses)
        self._candidates[candidate_id] = updated
        return updated

    def approve(self, candidate_id: str) -> CandidateRecord:
        record = self._get(candidate_id)
        self._require_phase(record, GovernancePhase.REVIEWED)
        if not record.unrestricted and (
            record.worktree is None
            or not record.worktree.clean
            or not record.worktree.isolated
            or not record.worktree.managed
        ):
            return self._reject(record, "clean_worktree_required")
        if not record.reproducer_evidence and not record.unrestricted:
            return self._reject(record, "reproducer_evidence_required")
        bypasses = record.bypassed_gates
        if not record.reproducer_evidence:
            bypasses = _append_once(bypasses, "reproducer")
        updated = replace(record, phase=GovernancePhase.APPROVED, bypassed_gates=bypasses)
        self._candidates[candidate_id] = updated
        return updated

    def deployment_intent(self, candidate_id: str, *, automatic_push: bool = False) -> DeploymentIntent:
        record = self._get(candidate_id)
        if automatic_push:
            return DeploymentIntent(candidate_id, False, "automatic_remote_push_forbidden")
        if record.phase is not GovernancePhase.APPROVED:
            return DeploymentIntent(candidate_id, False, f"deployment_requires_approval:{record.phase.value}", record.bypassed_gates)
        updated = replace(record, phase=GovernancePhase.DEPLOYMENT_INTENDED)
        self._candidates[candidate_id] = updated
        reason = "approved_with_explicit_bypasses" if record.bypassed_gates else "approved"
        return DeploymentIntent(candidate_id, True, reason, record.bypassed_gates)

    def get(self, candidate_id: str) -> CandidateRecord:
        return self._get(candidate_id)

    def _get(self, candidate_id: str) -> CandidateRecord:
        record = self._candidates.get(candidate_id)
        if record is None:
            raise NotFound(f"selfmod candidate {candidate_id!r} not found")
        return record

    @staticmethod
    def _require_phase(record: CandidateRecord, *expected: GovernancePhase) -> None:
        if record.phase is GovernancePhase.REJECTED:
            raise Forbidden("rejected candidate cannot advance")
        if record.phase not in expected:
            labels = ", ".join(item.value for item in expected)
            raise Conflict(f"candidate is {record.phase.value}; expected one of {labels}")

    def _reject(self, record: CandidateRecord, reason: str) -> CandidateRecord:
        updated = replace(record, phase=GovernancePhase.REJECTED, rejection_reason=reason)
        self._candidates[record.candidate_id] = updated
        return updated


def _append_once(values: tuple[str, ...], value: str) -> tuple[str, ...]:
    return values if value in values else values + (value,)


__all__ = [
    "CandidateRecord", "DeploymentIntent", "GovernanceInputError", "GovernancePhase",
    "ReviewEvidence", "SelfmodGovernance", "VerificationEvidence", "WorktreeMetadata",
]
