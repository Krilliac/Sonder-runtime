from __future__ import annotations

from datetime import datetime, timezone

import pytest

from sonder_runtime.domain.approval import (
    ApprovalDecision,
    ApprovalDecisionKind,
    ApprovalError,
    ApprovalRequest,
    resolve_approval,
)


ORIGINAL = "a" * 64


def test_approve_edit_and_reject_bind_to_request_identity():
    request = ApprovalRequest("approval-1", "file_write", ORIGINAL)
    approved = resolve_approval(request, ApprovalDecision("approval-1", "approve", "operator"))
    edited = resolve_approval(request, ApprovalDecision("approval-1", "edit", "operator", edited_arguments={"path": "safe.txt"}))
    rejected = resolve_approval(request, ApprovalDecision("approval-1", "reject", "operator", reason="not needed"))

    assert approved.accepted and approved.arguments_sha256 == ORIGINAL
    assert edited.accepted and edited.arguments["path"] == "safe.txt"
    assert edited.arguments_sha256 != ORIGINAL
    assert not rejected.accepted


def test_mismatched_expired_and_disallowed_decisions_fail_closed():
    request = ApprovalRequest(
        "approval-1", "file_write", ORIGINAL,
        allowed_decisions=frozenset({ApprovalDecisionKind.APPROVE}),
        expires_at="2020-01-01T00:00:00+00:00",
    )
    with pytest.raises(ApprovalError, match="expired"):
        resolve_approval(request, ApprovalDecision("approval-1", "approve", "operator"), now=datetime.now(timezone.utc))

    current = ApprovalRequest("approval-2", "file_write", ORIGINAL, allowed_decisions=frozenset({ApprovalDecisionKind.APPROVE}))
    with pytest.raises(ApprovalError, match="not allowed"):
        resolve_approval(current, ApprovalDecision("approval-2", "reject", "operator"))
    with pytest.raises(ApprovalError, match="does not match"):
        resolve_approval(current, ApprovalDecision("other", "approve", "operator"))


def test_edit_requires_json_object_arguments():
    with pytest.raises(ApprovalError, match="edited_arguments"):
        ApprovalDecision("approval-1", "edit", "operator", edited_arguments=["not", "object"])
