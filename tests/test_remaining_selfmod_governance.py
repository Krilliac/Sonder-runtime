"""Focused tests for the remaining self-modification governance boundary."""
from __future__ import annotations

import pytest

from sonder_runtime.application.selfmod.governance import (
    GovernanceInputError,
    GovernancePhase,
    ReviewEvidence,
    SelfmodGovernance,
    VerificationEvidence,
    WorktreeMetadata,
)
from sonder_runtime.application.selfmod.reproducer_contract import (
    AcceptanceCriterion,
    FailureEvidence,
)


DIGEST = "a" * 64


def _worktree(*, isolated: bool = True, clean: bool = True) -> WorktreeMetadata:
    return WorktreeMetadata("C:/worktrees/candidate", "selfmod/candidate", "abc123", isolated, clean)


def _verification(*, passed: bool = True) -> VerificationEvidence:
    return VerificationEvidence("test-1", "focused-tests", passed, "artifact-digest", "bounded suite")


def _review(*, approved: bool = True) -> ReviewEvidence:
    return ReviewEvidence("review-1", "independent-reviewer", approved, ("test-1",), "reviewed diff")


def _reproducer() -> FailureEvidence:
    return FailureEvidence(
        evidence_id="reproducer-1",
        command_argv=("python", "-m", "pytest", "tests/test_target.py", "-q"),
        expected_outcome="exit status 1 and signature E-TIMEOUT",
        artifact_digest=DIGEST,
        acceptance_criteria=(AcceptanceCriterion(
            "failure-reproduces",
            "The target failure is reproduced.",
            "exit status 1 and signature E-TIMEOUT",
        ),),
        failure_signature="E-TIMEOUT",
    )


def _approved_governance(*, unrestricted: bool = False) -> tuple[SelfmodGovernance, str]:
    governance = SelfmodGovernance()
    governance.propose("candidate-1", "improve recovery", "base-digest", unrestricted=unrestricted)
    governance.attach_worktree("candidate-1", _worktree())
    governance.record_reproducer("candidate-1", _reproducer())
    governance.record_verification("candidate-1", _verification())
    governance.record_review("candidate-1", _review())
    governance.approve("candidate-1")
    return governance, "candidate-1"


def test_guarded_lifecycle_requires_evidence_in_order_and_emits_local_intent():
    governance, candidate_id = _approved_governance()

    record = governance.get(candidate_id)
    intent = governance.deployment_intent(candidate_id)

    assert record.phase is GovernancePhase.APPROVED
    assert intent.allowed is True
    assert intent.reason == "approved"
    assert intent.automatic_push is False
    assert intent.remote_push_allowed is False


def test_guarded_candidate_rejects_nonisolated_worktree():
    governance = SelfmodGovernance()
    governance.propose("candidate-1", "change", "base")

    record = governance.attach_worktree("candidate-1", _worktree(isolated=False))

    assert record.phase is GovernancePhase.REJECTED
    assert record.rejection_reason == "isolated_managed_worktree_required"


def test_guarded_candidate_rejects_unmanaged_worktree():
    governance = SelfmodGovernance()
    governance.propose("candidate-1", "change", "base")

    record = governance.attach_worktree(
        "candidate-1", WorktreeMetadata("C:/worktrees/candidate", "branch", "commit", managed=False)
    )

    assert record.phase is GovernancePhase.REJECTED
    assert record.rejection_reason == "isolated_managed_worktree_required"


def test_worktree_metadata_is_required_and_explicit():
    with pytest.raises(GovernanceInputError):
        WorktreeMetadata("", "branch", "commit")


def test_failed_verification_blocks_guarded_candidate():
    governance = SelfmodGovernance()
    governance.propose("candidate-1", "change", "base")
    governance.attach_worktree("candidate-1", _worktree())

    record = governance.record_verification("candidate-1", _verification(passed=False))

    assert record.phase is GovernancePhase.REJECTED
    assert record.rejection_reason == "verification_failed:test-1"


def test_guarded_approval_requires_concrete_reproducer_evidence():
    governance = SelfmodGovernance()
    governance.propose("candidate-1", "change", "base")
    governance.attach_worktree("candidate-1", _worktree())
    governance.record_verification("candidate-1", _verification())
    governance.record_review("candidate-1", _review())

    record = governance.approve("candidate-1")

    assert record.phase is GovernancePhase.REJECTED
    assert record.rejection_reason == "reproducer_evidence_required"


def test_reproducer_contract_is_recorded_before_guarded_approval():
    governance = SelfmodGovernance()
    governance.propose("candidate-1", "change", "base")
    governance.attach_worktree("candidate-1", _worktree())

    record = governance.record_reproducer("candidate-1", _reproducer())

    assert record.phase is GovernancePhase.ISOLATED
    assert record.reproducer_evidence == (_reproducer(),)


def test_review_must_reference_known_verification_evidence():
    governance = SelfmodGovernance()
    governance.propose("candidate-1", "change", "base")
    governance.attach_worktree("candidate-1", _worktree())
    governance.record_verification("candidate-1", _verification())

    with pytest.raises(GovernanceInputError, match="unknown"):
        governance.record_review("candidate-1", ReviewEvidence("r", "reviewer", True, ("missing",)))


def test_rejected_review_blocks_guarded_candidate():
    governance = SelfmodGovernance()
    governance.propose("candidate-1", "change", "base")
    governance.attach_worktree("candidate-1", _worktree())
    governance.record_verification("candidate-1", _verification())

    record = governance.record_review("candidate-1", _review(approved=False))

    assert record.phase is GovernancePhase.REJECTED
    assert record.rejection_reason == "review_rejected"


def test_unrestricted_mode_records_bypasses_instead_of_falsely_passing_gates():
    governance = SelfmodGovernance()
    governance.propose("candidate-1", "emergency repair", "base", unrestricted=True)
    governance.attach_worktree("candidate-1", _worktree(isolated=False, clean=False))
    governance.record_verification("candidate-1", _verification(passed=False))
    governance.record_review("candidate-1", _review(approved=False))

    record = governance.approve("candidate-1")
    intent = governance.deployment_intent("candidate-1")

    assert record.phase is GovernancePhase.APPROVED
    assert set(record.bypassed_gates) == {"isolation", "verification", "review", "reproducer"}
    assert intent.allowed is True
    assert intent.reason == "approved_with_explicit_bypasses"
    assert set(intent.bypassed_gates) == set(record.bypassed_gates)


def test_unrestricted_mode_does_not_allow_missing_lifecycle_evidence():
    governance = SelfmodGovernance()
    governance.propose("candidate-1", "repair", "base", unrestricted=True)

    intent = governance.deployment_intent("candidate-1")

    assert intent.allowed is False
    assert "proposed" in intent.reason


def test_automatic_remote_push_is_always_refused_even_after_approval():
    governance, candidate_id = _approved_governance(unrestricted=True)

    intent = governance.deployment_intent(candidate_id, automatic_push=True)

    assert intent.allowed is False
    assert intent.reason == "automatic_remote_push_forbidden"
    assert governance.get(candidate_id).phase is GovernancePhase.APPROVED


def test_deployment_intent_is_idempotently_local_and_marks_intended_phase():
    governance, candidate_id = _approved_governance()

    first = governance.deployment_intent(candidate_id)
    second = governance.deployment_intent(candidate_id)

    assert first.allowed is True
    assert second.allowed is False
    assert "deployment_intended" in second.reason
