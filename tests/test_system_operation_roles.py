"""Role boundaries for system operations, independent of model prompt text.

Prompt injection can influence a model's proposed tool call, but it must never
be able to grant the caller the role required to execute a shared system
operation.  These checks cover the dispatch-independent HTTP authority gate.
"""

from __future__ import annotations

import sonder_runtime.interfaces.http.serve as serve
import server


def _context(role):
    return {
        "mode": "account", "authorized": True, "api_key": False,
        "account": {"username": role, "role": role},
    }


def test_every_declared_system_operation_has_an_explicit_role():
    assert set(serve.SYSTEM_OPERATION_ROLES) == {
        "permission_mode_change", "runtime_policy_change",
        "permission_rule_change", "account_management", "selfmod_deploy",
        "automation_lifecycle", "workspace_execution",
    }
    assert set(serve.SYSTEM_OPERATION_ROLES.values()) <= {"developer", "admin"}
    assert set(serve.SYSTEM_OPERATION_TOOLS.values()) <= set(
        serve.SYSTEM_OPERATION_ROLES
    )


def test_ordinary_user_is_refused_from_every_system_operation():
    for operation in serve.SYSTEM_OPERATION_ROLES:
        assert serve._system_operation_authority_error(operation, _context("user"))


def test_developer_cannot_cross_admin_boundary():
    for operation, role in serve.SYSTEM_OPERATION_ROLES.items():
        refusal = serve._system_operation_authority_error(operation, _context("developer"))
        assert bool(refusal) is (role == "admin"), operation


def test_admin_is_authorized_for_all_declared_system_operations():
    for operation in serve.SYSTEM_OPERATION_ROLES:
        assert serve._system_operation_authority_error(operation, _context("admin")) == ""


def test_local_open_trusted_operator_retains_extended_system_toolset():
    local_operator = {
        "mode": "local-open", "authorized": True, "api_key": False,
        "account": None,
    }
    for operation in serve.SYSTEM_OPERATION_ROLES:
        assert serve._system_operation_authority_error(operation, local_operator) == ""


def test_catalogued_system_tool_cannot_bypass_http_role_boundary():
    ordinary = _context("user")
    for tool, operation in serve.SYSTEM_OPERATION_TOOLS.items():
        refusal = serve._http_tool_refusal((tool,), "/" + tool, ordinary)
        assert "refused" in refusal and serve.SYSTEM_OPERATION_ROLES[operation] in (
            "developer", "admin",
        ), tool


def test_catalogued_dispatch_checks_role_before_invoking_system_tool(monkeypatch):
    def invoked(**_kwargs):
        raise AssertionError("system handler must not run for an ordinary account")

    monkeypatch.setattr(serve.server, "permission_mode", invoked)
    result = serve._dispatch_catalogued_tool(
        "/permission_mode auto", serve.ConversationState(), context=_context("user"),
    )
    assert result.startswith("refused /permission_mode:")
    assert "administrator authorization" in result


def test_runtime_slash_checks_admin_role_before_control_dispatch(monkeypatch):
    monkeypatch.setattr(serve, "_http_slash_refusal", lambda *_args, **_kwargs: "")
    monkeypatch.setattr(
        serve.server, "control_command",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("runtime control must not run for an ordinary account")
        ),
    )
    result = serve._handle_slash(
        "/runtime set code=qwen2.5-coder:14b",
        state=serve.ConversationState(), context=_context("developer"),
    )
    assert result.startswith("refused /runtime:")
    assert "administrator authorization" in result


def test_catalogued_system_tool_remains_available_to_local_open_operator():
    local_operator = {
        "mode": "local-open", "authorized": True, "api_key": False,
        "account": None,
    }
    for tool in serve.SYSTEM_OPERATION_TOOLS:
        refusal = serve._http_tool_refusal((tool,), "/" + tool, local_operator)
        # Permission modes may independently ask/deny a state-changing tool,
        # but the remote account-role boundary must not take authority away
        # from the trusted local operator surface.
        assert "administrator authorization" not in refusal, tool
        assert "developer or administrator authorization" not in refusal, tool


def test_agent_cannot_turn_injected_system_tool_names_into_authority():
    for tool in server._AGENT_SYSTEM_OPERATOR_TOOLS:
        result = server._agent_dispatch(tool, {}, allow_web=False)
        assert "system operation" in result.lower(), tool


def test_agent_help_does_not_advertise_system_operations():
    help_text = server._agent_tool_help()
    for tool in server._AGENT_SYSTEM_OPERATOR_TOOLS:
        assert "- %s:" % tool not in help_text, tool


def test_restricted_agent_policy_classifies_every_registered_tool():
    """A newly registered tool must not bypass the injection-policy predicate."""
    names = [tool.name for tool in server.mcp._tool_manager.list_tools()]
    assert len(names) >= 180
    for name in names:
        outcome = server._agent_run_tool_refusal(
            name, read_only=True, cloud=False, unsafe=False,
            project_bound=True, allow_web=False, allow_location=False,
        )
        assert isinstance(outcome, str), name


def test_every_system_tool_first_leg_blocks_all_registered_follow_on_tools():
    """No injected privileged first call can reach a second tool in a chain."""
    names = [tool.name for tool in server.mcp._tool_manager.list_tools()]
    for protected in server._AGENT_SYSTEM_OPERATOR_TOOLS:
        for follow_on in names:
            # The combination is represented by a model proposing protected
            # first, then follow_on.  The first leg must refuse without
            # inspecting or invoking either argument payload.
            outcome = server._agent_dispatch(
                protected, {"then": follow_on}, allow_web=False,
            )
            assert "system operation" in outcome.lower(), (
                protected, follow_on,
            )
