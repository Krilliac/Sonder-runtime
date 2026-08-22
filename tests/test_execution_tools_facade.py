from __future__ import annotations

import pytest

from sonder_runtime.application.execution.facade import ExecutionApplicationFacade
from sonder_runtime.application.execution.world_control import ExecutionSurface
from sonder_runtime.application.ports.tool_execution import ToolExecutionResult
from sonder_runtime.application.ports.tool_registry import InMemoryToolRegistry, ToolDescriptor
from sonder_runtime.application.tools.facade import ToolApplicationFacade
from sonder_runtime.application.tools.gateway_contract import ApprovalMode, ToolGatewayRequest, ToolPermission, ToolScope
from sonder_runtime.application.tools.resource_policy import Decision, PolicyRule, ResourcePolicy
from sonder_runtime.domain.common.errors import Forbidden


class _Executor:
    def execute(self, descriptor, call, context, execution_class):
        return ToolExecutionResult(descriptor.name, True, output={"ok": True})


def test_execution_facade_uses_one_world_and_is_not_executable_without_provider():
    facade = ExecutionApplicationFacade.local()
    assert facade.executable is False
    assert facade.bind(ExecutionSurface.SHELL).world_id == facade.world.world_id
    assert facade.world.isolation.truth.value == "unverified"


def test_tool_facade_derives_catalogs_and_denies_without_a_policy_match():
    registry = InMemoryToolRegistry((ToolDescriptor("status"),))
    facade = ToolApplicationFacade.compose(registry, _Executor())
    assert [item["name"] for item in facade.catalogs.mcp["tools"]] == ["status"]
    with pytest.raises(Forbidden):
        facade.execute(ToolGatewayRequest(
            request_id="req-1",
            tool_name="status",
            arguments={},
            scope=ToolScope("owner"),
            permission=ToolPermission(),
        ))


def test_tool_facade_keeps_approval_as_an_explicit_second_gate():
    registry = InMemoryToolRegistry((ToolDescriptor("status"),))
    policy = ResourcePolicy((PolicyRule("status", Decision.ALLOW, tool="status"),))
    facade = ToolApplicationFacade.compose(registry, _Executor(), policy=policy)
    request = ToolGatewayRequest(
        request_id="req-2", tool_name="status", arguments={}, scope=ToolScope("owner"),
        permission=ToolPermission(frozenset(), approval=ApprovalMode.REQUIRED), approval_token="token",
    )
    with pytest.raises(Forbidden):
        facade.execute(request)


def test_tool_receipt_is_truthful_for_unredacted_default_output():
    registry = InMemoryToolRegistry((ToolDescriptor("status"),))
    policy = ResourcePolicy((PolicyRule("status", Decision.ALLOW, tool="status"),))
    facade = ToolApplicationFacade.compose(registry, _Executor(), policy=policy)
    receipt = facade.execute(ToolGatewayRequest(
        request_id="req-3", tool_name="status", arguments={}, scope=ToolScope("owner"),
        permission=ToolPermission(), execution_world="local-execution",
    ))
    assert receipt.success is True
    assert receipt.redaction_applied is False
    assert receipt.requester_id == "owner"
    assert receipt.execution_world == "local-execution"
    assert len(receipt.argument_digest) == 64
    assert len(receipt.result_digest) == 64
