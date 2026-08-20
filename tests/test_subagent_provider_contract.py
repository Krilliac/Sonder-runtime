import pytest

from sonder_runtime.application.ports.subagents import (
    InvalidSubagentRequest,
    SubagentBudget,
    SubagentError,
    SubagentProtocolError,
    SubagentResult,
    SubagentStatus,
    validate_child_budget,
)


def test_budget_requires_a_ceiling_and_rejects_invalid_values():
    with pytest.raises(InvalidSubagentRequest):
        SubagentBudget()
    with pytest.raises(InvalidSubagentRequest):
        SubagentBudget(max_steps=0)
    with pytest.raises(InvalidSubagentRequest):
        SubagentBudget(max_output_tokens=1.5)


def test_child_budget_cannot_widen_parent():
    parent = SubagentBudget(max_steps=10, max_wall_seconds=5.0)
    validate_child_budget(SubagentBudget(max_steps=4, max_wall_seconds=2.0), parent)
    with pytest.raises(InvalidSubagentRequest, match="max_steps"):
        validate_child_budget(SubagentBudget(max_steps=11), parent)
    with pytest.raises(InvalidSubagentRequest, match="max_wall_seconds"):
        validate_child_budget(
            SubagentBudget(max_steps=10, max_wall_seconds=6.0), parent
        )


def test_result_protocol_requires_terminal_and_consistent_error():
    with pytest.raises(SubagentProtocolError, match="terminal"):
        SubagentResult("child", "parent", SubagentStatus.RUNNING)
    with pytest.raises(SubagentProtocolError, match="requires an error"):
        SubagentResult("child", "parent", SubagentStatus.CANCELLED)
    result = SubagentResult(
        "child", "parent", SubagentStatus.CANCELLED,
        error=SubagentError("cancelled", "parent requested cancellation"),
    )
    assert result.parent_id == "parent"
    assert result.status is SubagentStatus.CANCELLED


def test_result_success_has_no_error_and_preserves_output():
    result = SubagentResult("child", "parent", SubagentStatus.SUCCEEDED, output="done")
    assert result.output == "done"
    assert result.error is None
