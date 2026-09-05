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


def test_consumption_exception_never_becomes_resumable_pending(tmp_path):
    class UncertainLedger(ApprovalLedger):
        def consume(self, *args, **kwargs):
            super().consume(*args, **kwargs)
            raise OSError("lost response")
    ledger = UncertainLedger(tmp_path / "approvals.db")
    issued = ledger.issue("workspace_run", permission_modes.call_digest("workspace_run", {}),
                          approver="operator")
    with pytest.raises(PermissionError, match="APPROVAL_OUTCOME_UNKNOWN"):
        bridge(ledger).authorize("workspace_run", {}, surface="agent", expires_at=time.time() + 60)
    assert ledger.get(issued.nonce).spent
    assert ledger.pending() == []


def test_runtime_home_change_cannot_redirect_decision_ledger(tmp_path, monkeypatch):
    from sonder_runtime.adapters.security import approval_ledger as module
    selected = {"path": str(tmp_path / "first.db")}
    monkeypatch.setattr(module.runtime_paths, "state_path", lambda *args: selected["path"])
    ledger = ApprovalLedger()
    args = {"program": "python"}
    issued = ledger.issue("workspace_run", permission_modes.call_digest("workspace_run", args), approver="operator")
    def decide(tool, **kwargs):
        selected["path"] = str(tmp_path / "second.db")
        return permission_modes.decide(tool, interactive=False, mode="manual", rule_lookup=lambda _: None, **kwargs)
    gate = ContinuationApprovalBridge(ledger=ledger, decide=decide, digest_call=permission_modes.call_digest)
    allowed = gate.authorize("workspace_run", args, surface="agent", expires_at=time.time() + 60)
    assert allowed.approval_nonce == issued.nonce
    assert not (tmp_path / "second.db").exists()


def test_policy_decides_frozen_copy_of_caller_arguments(tmp_path):
    args = {"program": "python", "args": ["original"]}
    digest = permission_modes.call_digest("workspace_run", args)
    def decide(tool, **kwargs):
        args["args"][0] = "changed"
        assert kwargs["arguments"]["args"] == ["original"]
        return permission_modes.decide(tool, interactive=False, mode="manual",
            rule_lookup=lambda _: {"action": "allow", "pattern": tool}, **kwargs)
    gate = ContinuationApprovalBridge(ledger=ApprovalLedger(tmp_path / "approvals.db"),
        decide=decide, digest_call=permission_modes.call_digest)
    result = gate.authorize("workspace_run", args, surface="agent", expires_at=time.time() + 60)
    assert result.call_digest == digest
    assert result.decision_id.startswith("policy:rule:")
