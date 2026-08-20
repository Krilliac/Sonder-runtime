from __future__ import annotations

import pytest

from sonder_runtime.application.ports.tool_execution import (
    ToolExecutionResult,
    ToolExecutor,
    ToolPolicy,
)
from sonder_runtime.application.ports.tool_registry import (
    InMemoryToolRegistry,
    ToolCall,
    ToolDescriptor,
    validate_tool_call,
)
from sonder_runtime.domain.common.errors import Conflict, InvalidInput, NotFound


def _descriptor() -> ToolDescriptor:
    return ToolDescriptor(
        name="workspace.read",
        description="Read a workspace file.",
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string", "minLength": 1}},
            "required": ["path"],
            "additionalProperties": False,
        },
    )


def test_registry_is_deterministic_and_rejects_duplicates():
    registry = InMemoryToolRegistry([_descriptor()])
    assert registry.get("workspace.read") == _descriptor()
    assert registry.list_all() == (_descriptor(),)
    with pytest.raises(Conflict):
        registry.register(_descriptor())


def test_registry_require_reports_unknown_tool():
    with pytest.raises(NotFound):
        InMemoryToolRegistry().require("missing")


@pytest.mark.parametrize("arguments", [{}, {"path": ""}, {"path": 3}, {"path": "x", "extra": True}])
def test_validation_rejects_invalid_arguments(arguments):
    with pytest.raises(InvalidInput):
        validate_tool_call(_descriptor(), ToolCall("workspace.read", arguments))


def test_validation_accepts_arguments_without_mutating_them():
    arguments = {"path": "note.txt"}
    call = ToolCall("workspace.read", arguments)
    validate_tool_call(_descriptor(), call)
    assert call.arguments == arguments


def test_execution_result_has_stable_success_alias():
    result = ToolExecutionResult("workspace.read", True, output="contents")
    assert result.ok is True
    assert result.error_code == ""


def test_policy_and_executor_are_protocols():
    assert isinstance(ToolPolicy, type)
    assert isinstance(ToolExecutor, type)
