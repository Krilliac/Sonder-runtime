from __future__ import annotations

import pytest

from sonder_runtime.domain.mode_tool_policy import (
    AgentMode,
    ToolDisposition,
    project_mode_tool_policy,
)


TOOLS = ("read_file", "grep", "write_file", "run_command")
READ_ONLY = ("read_file", "grep")
MUTATING = ("write_file", "run_command")


@pytest.mark.parametrize("mode", [AgentMode.CHAT, "plan", "agent"])
def test_modes_have_explicit_tool_dispositions(mode):
    policy = project_mode_tool_policy(mode, TOOLS, read_only_tools=READ_ONLY, mutating_tools=MUTATING)

    if policy.mode is AgentMode.CHAT:
        assert policy.available_tools() == ()
    elif policy.mode is AgentMode.PLAN:
        assert policy.available_tools() == tuple(sorted(READ_ONLY))
        assert policy.disposition("write_file") is ToolDisposition.EXCLUDED
    else:
        assert policy.disposition("read_file") is ToolDisposition.AUTOMATIC
        assert policy.disposition("write_file") is ToolDisposition.APPROVAL_REQUIRED
    assert policy.disposition("unknown") is ToolDisposition.EXCLUDED


def test_unclassified_or_overlapping_tools_fail_closed():
    with pytest.raises(ValueError, match="classified"):
        project_mode_tool_policy("plan", ("read", "write"), read_only_tools=("read",))
    with pytest.raises(ValueError, match="both"):
        project_mode_tool_policy("agent", ("read",), read_only_tools=("read",), mutating_tools=("read",))
