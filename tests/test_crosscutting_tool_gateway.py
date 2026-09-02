from __future__ import annotations

import time

import pytest

from sonder_runtime.application.tools.gateway_contract import (
    ApprovalMode, ToolGateway, ToolGatewayRequest, ToolInvocationOutput,
    ToolPermission, ToolScope,
)
from sonder_runtime.domain.common.errors import Cancelled, DeadlineExceeded, Forbidden, InvalidInput


class Cancel:
    cancelled = False


class Schema:
    def __init__(self, events): self.events = events
    def validate(self, name, arguments): self.events.append(("schema", name, dict(arguments)))


class Permissions:
    def __init__(self, events): self.events = events
    def authorize(self, name, scope, permission): self.events.append(("permission", name))


class Approvals:
    def __init__(self, events, result=True): self.events, self.result = events, result
    def approve(self, request): self.events.append("approval"); return self.result


class Invoker:
    def __init__(self, events): self.events = events
    def invoke(self, request): self.events.append("invoke"); return ToolInvocationOutput(True, {"token": "secret", "ok": 1})


class Redactor:
    def __init__(self, events): self.events = events
    def redact(self, name, output): self.events.append("redact"); return {"token": "[redacted]", "ok": output["ok"]}


class Receipts:
    def __init__(self, events): self.events, self.items = events, []
    def record(self, receipt): self.events.append("receipt"); self.items.append(receipt)


def make_gateway(events, approval=True):
    receipts = Receipts(events)
    return ToolGateway(Schema(events), Permissions(events), Approvals(events, approval), Invoker(events), Redactor(events), receipts), receipts


def request(**kwargs):
    return ToolGatewayRequest(
        request_id="r1", tool_name="read", arguments={"path": "x"},
        scope=ToolScope("owner", ("project",), frozenset({"read"})),
        permission=ToolPermission(frozenset({"read"}), kwargs.pop("approval", ApprovalMode.NOT_REQUIRED)),
        **kwargs,
    )


def test_pipeline_orders_checks_redaction_and_receipt():
    events = []
    gateway, receipts = make_gateway(events)
    receipt = gateway.execute(request())
    assert events == [("schema", "read", {"path": "x"}), ("permission", "read"), "invoke", "redact", "receipt"]
    assert receipt.output["token"] == "[redacted]" and receipts.items == [receipt]


def test_required_approval_is_explicit_and_blocks_invocation():
    events = []
    gateway, receipts = make_gateway(events, approval=False)
    with pytest.raises(Forbidden):
        gateway.execute(request(approval=ApprovalMode.REQUIRED, approval_token="approval-1"))
    # the refusal is an outcome: it leaves a receipt that says so
    assert events == [("schema", "read", {"path": "x"}), ("permission", "read"), "approval", "receipt"]
    assert receipts.items[-1].terminal == "policy_denied"
    assert receipts.items[-1].success is False


def test_deadline_and_cancellation_are_checked_before_provider_boundary():
    events = []
    gateway, receipts = make_gateway(events)
    with pytest.raises(DeadlineExceeded):
        gateway.execute(request(deadline_monotonic=time.monotonic() - 1))
    token = Cancel(); token.cancelled = True
    with pytest.raises(Cancelled):
        gateway.execute(request(cancellation=token))
    # nothing past the control point ran, and each exit still left its receipt
    assert events == ["receipt", "receipt"]
    assert [item.terminal for item in receipts.items] == ["deadline_exceeded", "cancelled"]


def test_scope_and_permission_are_typed_and_immutable():
    scope = ToolScope("owner", ("project",), frozenset({"read"}))
    permission = ToolPermission(frozenset({"read"}), ApprovalMode.REQUIRED)
    assert scope.principal_id == "owner" and permission.approval is ApprovalMode.REQUIRED
    with pytest.raises(InvalidInput): ToolScope("")
    with pytest.raises(InvalidInput): ToolPermission(frozenset({""}))


def test_permission_effects_cannot_exceed_request_scope():
    events = []
    gateway, _ = make_gateway(events)
    with pytest.raises(Forbidden, match="exceeds the request scope"):
        gateway.execute(ToolGatewayRequest(
            request_id="r-scope",
            tool_name="write",
            arguments={},
            scope=ToolScope("owner", ("project",), frozenset({"read"})),
            permission=ToolPermission(frozenset({"write"})),
        ))
    assert events == [("schema", "write", {}), "receipt"]
