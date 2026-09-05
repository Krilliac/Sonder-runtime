import time

import pytest
import permission_modes

from sonder_runtime.adapters.security.approval_ledger import ApprovalLedger
from sonder_runtime.adapters.security.continuation_approval import ContinuationApprovalBridge
from sonder_runtime.application.ports.lane_continuation import VerificationApprovalPending


def bridge(ledger, *, rule=None):
    def decide(tool, **kwargs):
        return permission_modes.decide(tool, interactive=False, mode="manual",
                                       rule_lookup=lambda _: rule, **kwargs)
    return ContinuationApprovalBridge(ledger=ledger, decide=decide,
                                      digest_call=permission_modes.call_digest)


def test_real_ask_approve_and_exact_consumed_nonce(tmp_path):
    ledger = ApprovalLedger(tmp_path / "approvals.db")
    gate = bridge(ledger)
    args = {"program": "python", "args": ["-m", "pytest"]}
    expiry = time.time() + 120
    with pytest.raises(VerificationApprovalPending) as pending:
        gate.authorize("workspace_run", args, surface="agent", expires_at=expiry)
    evidence = pending.value.evidence
    row = ledger.resolve_call(evidence.call_digest)
    issued = ledger.issue(row.tool, row.digest, approver="operator", surface="repl")
    allowed = gate.authorize("workspace_run", args, surface="agent", expires_at=expiry)
    assert allowed.source == "approval"
    assert allowed.approval_nonce == issued.nonce
    assert ledger.get(issued.nonce).spent
    assert allowed.expires_at <= expiry
    with pytest.raises(VerificationApprovalPending):
        gate.authorize("workspace_run", args, surface="agent", expires_at=expiry)


def test_failed_pending_write_is_not_pending_evidence(tmp_path):
    class FailingLedger(ApprovalLedger):
        def record_pending(self, *args, **kwargs):
            raise OSError("unavailable")
    gate = bridge(FailingLedger(tmp_path / "approvals.db"))
    with pytest.raises(PermissionError):
        gate.authorize("workspace_run", {}, surface="agent", expires_at=time.time() + 60)


def test_expired_authority_does_not_consume_approval(tmp_path):
    ledger = ApprovalLedger(tmp_path / "approvals.db")
    args = {"program": "python"}
    issued = ledger.issue("workspace_run", permission_modes.call_digest("workspace_run", args),
                          approver="operator")
    with pytest.raises(PermissionError):
        bridge(ledger).authorize("workspace_run", args, surface="agent", expires_at=time.time() - 1)
    assert not ledger.get(issued.nonce).spent


def test_rule_allow_is_policy_evidence_without_approval_nonce(tmp_path):
    gate = bridge(ApprovalLedger(tmp_path / "approvals.db"), rule={"action": "allow", "pattern": "workspace_run"})
    allowed = gate.authorize("workspace_run", {}, surface="agent", expires_at=time.time() + 60)
    assert allowed.source == "policy"
    assert allowed.approval_nonce == ""
    assert allowed.decision_id
