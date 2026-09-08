"""Typed host ledger evidence cannot conflate pending, policy, or actual spend."""
from dataclasses import replace
import pytest
from sonder_runtime.application.ports.lane_continuation import (
    PendingApprovalEvidence, GrantedApprovalEvidence, VerificationApprovalPending,
)


def test_pending_identity_is_exact_and_has_no_spend_nonce():
    pending = PendingApprovalEvidence("workspace_run", "a"*64, "agent", "a"*16, 100.0)
    assert VerificationApprovalPending(pending).evidence is pending
    assert not hasattr(pending, "approval_nonce")
    with pytest.raises(ValueError):
        replace(pending, call_id="b"*16)
    with pytest.raises(TypeError):
        VerificationApprovalPending({"call_id": "a"*16})


@pytest.mark.parametrize("change", [dict(source="allow"), dict(approval_nonce=""),
    dict(expires_at=float("nan")), dict(expires_at=True), dict(call_digest="A"*64), dict(surface="")])
def test_granted_evidence_rejects_invalid_spend_or_identity(change):
    grant = GrantedApprovalEvidence("workspace_run", "a"*64, "agent", "decision", "actual-nonce", 100.0, "approval")
    with pytest.raises(ValueError):
        replace(grant, **change)


def test_policy_decision_is_explicit_and_has_no_fabricated_nonce():
    grant = GrantedApprovalEvidence("workspace_run", "a"*64, "agent", "policy-revision", "", 100.0, "policy")
    with pytest.raises(ValueError):
        replace(grant, approval_nonce="synthetic")
