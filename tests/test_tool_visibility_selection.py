from __future__ import annotations

from contextvars import copy_context

import pytest

from sonder_runtime.application.ports.tool_execution import ToolExecutionResult
from sonder_runtime.application.ports.tool_registry import (
    InMemoryToolRegistry,
    ToolDescriptor,
    ToolSchemaSelection,
)
from sonder_runtime.application.tools.gateway_contract import (
    ToolGateway,
    ToolGatewayRequest,
    ToolPermission,
    ToolScope,
)
from sonder_runtime.application.tools.generated_catalogs import GeneratedCatalogs
from sonder_runtime.domain.common.errors import Forbidden
from sonder_runtime.domain.common.errors import InvalidInput


class _Policy:
    def authorize(self, descriptor, call, context):
        return None

    def select_execution_class(self, descriptor):
        return descriptor.execution_class


class _Executor:
    def __init__(self):
        self.calls = []

    def execute(self, descriptor, call, context, execution_class):
        self.calls.append(descriptor.name)
        return ToolExecutionResult(descriptor.name, True, output={"ok": True})


class _Permissions:
    def authorize(self, tool_name, scope, permission):
        return "test"


class _Approvals:
    def approve(self, request):
        return True


class _Redactor:
    def redact(self, name, output):
        return output


class _Receipts:
    def __init__(self):
        self.items = []

    def record(self, receipt):
        self.items.append(receipt)


def _request(name, selection):
    return ToolGatewayRequest(
        request_id="visibility-1",
        tool_name=name,
        arguments={},
        scope=ToolScope("owner", allowed_effects=frozenset()),
        permission=ToolPermission(),
        schema_selection=selection,
    )


def _gateway():
    registry = InMemoryToolRegistry((ToolDescriptor("public"), ToolDescriptor("hidden")))
    executor = _Executor()
    receipts = _Receipts()
    gateway = ToolGateway.from_typed_ports(
        registry, _Policy(), executor, _Permissions(), _Approvals(), _Redactor(), receipts
    )
    return gateway, executor


def test_registered_hidden_tool_is_refused_before_executor():
    gateway, executor = _gateway()
    selection = ToolSchemaSelection({"public"}, selection_id="turn-1")

    with pytest.raises(Forbidden, match="not visible"):
        gateway.execute(_request("hidden", selection))

    assert executor.calls == []


def test_immutable_selection_survives_bounded_context_reset():
    gateway, executor = _gateway()
    selection = ToolSchemaSelection({"public"}, selection_id="turn-2")
    request = _request("public", selection)

    # Carry the same immutable selection through a copied context.  No global
    # ContextVar is used, so resetting the bounded context cannot widen it.
    copied = copy_context()
    result = copied.run(gateway.execute, request)

    assert result.success is True
    assert executor.calls == ["public"]
    assert selection.marker()["visible_names"] == ("public",)


def test_selected_catalog_omits_hidden_schemas_and_changes_digest():
    registry = InMemoryToolRegistry((
        ToolDescriptor("public", input_schema={"type": "object"}),
        ToolDescriptor("hidden", input_schema={"type": "object", "secret": True}),
    ))
    selected = GeneratedCatalogs.generate(
        registry, event_kinds=[], selection=ToolSchemaSelection({"public"})
    )
    full = GeneratedCatalogs.generate(registry, event_kinds=[])

    assert [item["name"] for item in selected.mcp["tools"]] == ["public"]
    assert [item["name"] for item in selected.summary["tools"]] == ["hidden", "public"]
    assert [item["name"] for item in selected.client["tools"]] == ["public"]
    assert [item["function"]["name"] for item in selected.openai["tools"]] == ["public"]
    assert selected.digest != full.digest


def test_request_rejects_untyped_schema_selection_before_admission():
    with pytest.raises(InvalidInput, match="ToolSchemaSelection"):
        _request("public", "public")
