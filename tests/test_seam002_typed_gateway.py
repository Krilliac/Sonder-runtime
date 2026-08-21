from __future__ import annotations

import time

import pytest

from sonder_runtime.application.ports.tool_execution import ToolExecutionResult
from sonder_runtime.application.ports.tool_registry import (
    InMemoryToolRegistry,
    ToolCall,
    ToolDescriptor,
)
from sonder_runtime.application.tools.gateway_contract import (
    ApprovalMode,
    ToolGateway,
    ToolGatewayRequest,
    ToolPermission,
    ToolScope,
)
from sonder_runtime.domain.common.errors import Cancelled, DeadlineExceeded, Forbidden
from sonder_runtime.domain.tools.descriptors import ExecutionClass


class _Cancel:
    cancelled = False


class _Policy:
    def __init__(self, events):
        self.events = events

    def authorize(self, descriptor, call, context):
        self.events.append(("policy", descriptor.name, call.arguments, context.principal_id))

    def select_execution_class(self, descriptor):
        self.events.append(("class", descriptor.name))
        return descriptor.execution_class


class _Executor:
    def __init__(self, events):
        self.events = events

    def execute(self, descriptor, call, context, execution_class):
        self.events.append(("executor", descriptor.name, call.arguments, execution_class))
        return ToolExecutionResult(
            descriptor.name, True, output={"secret": "raw", "path": call.arguments["path"]},
            metadata={"source": context.source},
        )


class _SchemaPermission:
    def __init__(self, events):
        self.events = events

    def validate(self, name, arguments):
        self.events.append(("gateway-schema", name))


class _Permissions:
    def __init__(self, events):
        self.events = events

    def authorize(self, name, scope, permission):
        self.events.append(("permission", name, scope.principal_id, permission.effects))


class _Approvals:
    def __init__(self, events, allowed=True):
        self.events, self.allowed = events, allowed

    def approve(self, request):
        self.events.append("approval")
        return self.allowed


class _Redactor:
    def __init__(self, events):
        self.events = events

    def redact(self, name, output):
        self.events.append("redact")
        return {**output, "secret": "[redacted]"}


class _Receipts:
    def __init__(self, events):
        self.events, self.items = events, []

    def record(self, receipt):
        self.events.append("receipt")
        self.items.append(receipt)


def _request(**overrides):
    values = dict(
        request_id="seam002-1",
        tool_name="workspace.read",
        arguments={"path": "note.txt"},
        scope=ToolScope("principal-7", ("workspace",), frozenset({"read"})),
        permission=ToolPermission(frozenset({"read"})),
    )
    values.update(overrides)
    return ToolGatewayRequest(**values)


def _gateway(events):
    registry = InMemoryToolRegistry((
        ToolDescriptor(
            "workspace.read",
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string", "minLength": 1}},
                "required": ["path"],
                "additionalProperties": False,
            },
            execution_class=ExecutionClass.PURE,
        ),
    ))
    receipts = _Receipts(events)
    return ToolGateway.from_typed_ports(
        registry,
        _Policy(events),
        _Executor(events),
        _Permissions(events),
        _Approvals(events),
        _Redactor(events),
        receipts,
    ), receipts


def test_typed_ports_preserve_gateway_pipeline_and_scope():
    events = []
    gateway, receipts = _gateway(events)
    result = gateway.execute(_request())

    assert result.output == {"secret": "[redacted]", "path": "note.txt"}
    assert receipts.items == [result]
    assert events == [
        ("permission", "workspace.read", "principal-7", frozenset({"read"})),
        ("policy", "workspace.read", {"path": "note.txt"}, "principal-7"),
        ("class", "workspace.read"),
        ("executor", "workspace.read", {"path": "note.txt"}, ExecutionClass.PURE),
        "redact",
        "receipt",
    ]


def test_typed_registry_schema_rejects_before_policy_or_executor():
    events = []
    gateway, _ = _gateway(events)
    with pytest.raises(Exception):
        gateway.execute(_request(arguments={"path": ""}))
    assert [event for event in events if isinstance(event, tuple) and event[0] in {"policy", "executor"}] == []


def test_approval_deadline_and_cancellation_still_fail_closed():
    events = []
    gateway, _ = _gateway(events)
    with pytest.raises(Forbidden):
        gateway.execute(_request(
            permission=ToolPermission(frozenset({"read"}), ApprovalMode.REQUIRED),
            approval_token=None,
        ))
    with pytest.raises(DeadlineExceeded):
        gateway.execute(_request(deadline_monotonic=time.monotonic() - 1))
    token = _Cancel()
    token.cancelled = True
    with pytest.raises(Cancelled):
        gateway.execute(_request(cancellation=token))
