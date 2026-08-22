from __future__ import annotations

import json

import pytest

from sonder_runtime.adapters.persistence.tool_audit import (
    DurableToolAuditRepository,
    ToolAuditLimits,
)
from sonder_runtime.application.tools.gateway_contract import (
    ApprovalMode,
    ToolGateway,
    ToolGatewayRequest,
    ToolInvocationOutput,
    ToolPermission,
    ToolScope,
)
from sonder_runtime.application.tools.audit import ToolAuditError
from sonder_runtime.domain.common.errors import InvalidInput
from sonder_runtime.platform.logging import REDACTION_FAILED


class _Schema:
    def validate(self, _name, _arguments): pass


class _Permissions:
    def authorize(self, _name, _scope, _permission): pass


class _Approvals:
    def approve(self, _request): return True


class _Invoker:
    def invoke(self, _request):
        return ToolInvocationOutput(True, {"token": "secret", "value": 3})


class _GatewayRedactor:
    def redact(self, _name, output):
        return {"token": "[redacted]", "value": output["value"]}


class _Receipts:
    def __init__(self): self.items = []
    def record(self, receipt): self.items.append(receipt)


class _FailingRedactor:
    def redact(self, _text): return REDACTION_FAILED


def _request(*, session="session-1", project="project-1"):
    return ToolGatewayRequest(
        request_id="request-1", tool_name="read", arguments={"path": "x"},
        scope=ToolScope("owner", ("project-root",), frozenset({"read"})),
        permission=ToolPermission(frozenset({"read"}), ApprovalMode.NOT_REQUIRED),
        session_id=session, project_id=project,
    )


def _gateway(audit, receipts):
    return ToolGateway(_Schema(), _Permissions(), _Approvals(), _Invoker(), _GatewayRedactor(), receipts, audit=audit)


def test_gateway_persists_redacted_receipt_with_scope_and_chain(tmp_path):
    repository = DurableToolAuditRepository(tmp_path / "tool-audit.jsonl")
    receipts = _Receipts()
    gateway = _gateway(repository, receipts)

    first = gateway.execute(_request())
    second = gateway.execute(_request(session="session-2", project="project-2"))

    records = repository.read()
    assert len(records) == 2
    assert records[0]["session_id"] == "session-1"
    assert records[1]["project_id"] == "project-2"
    assert records[0]["output"]["token"] == "[redacted]"
    assert "secret" not in json.dumps(records)
    assert records[0]["previous_audit_digest"] == ""
    assert records[1]["previous_audit_digest"] == records[0]["audit_digest"]
    repository.verify()
    assert receipts.items == [first, second]


def test_audit_redaction_failure_is_fail_closed_before_receipt_publication(tmp_path):
    repository = DurableToolAuditRepository(tmp_path / "tool-audit.jsonl", redactor=_FailingRedactor())
    receipts = _Receipts()
    with pytest.raises(ToolAuditError):
        _gateway(repository, receipts).execute(_request())
    assert receipts.items == []
    assert not repository.path.exists()


def test_audit_detects_tampering_and_enforces_bounds(tmp_path):
    path = tmp_path / "tool-audit.jsonl"
    repository = DurableToolAuditRepository(path, limits=ToolAuditLimits(max_records=1, max_bytes=4096))
    receipts = _Receipts()
    _gateway(repository, receipts).execute(_request())
    with pytest.raises(ToolAuditError, match="record bound"):
        _gateway(repository, receipts).execute(_request(session="other"))
    row = json.loads(path.read_text())
    row["project_id"] = "tampered"
    path.write_text(json.dumps(row) + "\n")
    with pytest.raises(ToolAuditError, match="integrity"):
        repository.verify()


def test_scope_identifiers_reject_blank_values():
    with pytest.raises(InvalidInput):
        _request(session=" ")
