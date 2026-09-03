"""Boundary tests for sonder_runtime.domain.cloud_agent_tool_policy."""

import server
from sonder_runtime.domain.cloud_agent_tool_policy import (
    cloud_agent_tool_policy_error,
)


LOCAL_ONLY = frozenset({"file_read", "repo_status"})
NESTED_MODEL = frozenset({"offload", "workflow_run"})


def test_server_delegate_uses_domain_function():
    result = server._cloud_agent_tool_policy_error("__nonexistent_tool__")
    assert result == ""


def test_local_only_tool_refused():
    result = cloud_agent_tool_policy_error(
        "file_read", local_only_tools=LOCAL_ONLY, nested_model_tools=NESTED_MODEL,
    )
    assert "HOST POLICY" in result
    assert "local-only" in result
    assert "file_read" in result


def test_local_only_not_bypassed_by_unsafe():
    result = cloud_agent_tool_policy_error(
        "file_read", unsafe=True,
        local_only_tools=LOCAL_ONLY, nested_model_tools=NESTED_MODEL,
    )
    assert result != ""


def test_nested_model_tool_refused():
    result = cloud_agent_tool_policy_error(
        "offload", local_only_tools=LOCAL_ONLY, nested_model_tools=NESTED_MODEL,
    )
    assert "HOST POLICY" in result
    assert "nested model-spawning" in result


def test_nested_model_tool_bypassed_by_unsafe():
    result = cloud_agent_tool_policy_error(
        "offload", unsafe=True,
        local_only_tools=LOCAL_ONLY, nested_model_tools=NESTED_MODEL,
    )
    assert result == ""


def test_allowed_tool_returns_empty():
    result = cloud_agent_tool_policy_error(
        "status", local_only_tools=LOCAL_ONLY, nested_model_tools=NESTED_MODEL,
    )
    assert result == ""


def test_empty_sets_allow_all():
    result = cloud_agent_tool_policy_error("anything")
    assert result == ""
