"""Role tool gate on the agent dispatch path (public-mode P1/P2).

A served caller's role is bound for the handler thread; every agent dispatch
started on its behalf (web chat, computer-use chat route) meets the same
allowlist the HTTP ``/<tool>`` path does.
"""

from __future__ import annotations

import pytest

import permission_modes
import server
from sonder_runtime.adapters.security import role_tool_policy
from sonder_runtime.bootstrap import computer_use_chat


@pytest.fixture
def user_caller():
    token = role_tool_policy._CALLER_ROLE.set("user")
    yield
    role_tool_policy._CALLER_ROLE.reset(token)


@pytest.fixture
def auto_mode(monkeypatch):
    monkeypatch.setattr(permission_modes, "_STATE", {
        **permission_modes._STATE, "mode": "auto", "elevated": False,
    })
    monkeypatch.setattr(permission_modes, "_LOADED", True)


def _record(monkeypatch, *names):
    called = []
    for name in names:
        monkeypatch.setattr(
            server, name, (lambda n: lambda *a, **k: (called.append(n), "ran")[1])(name),
            raising=False,
        )
    return called


@pytest.mark.parametrize("tool", ("file_write", "run_code", "ui_action", "web_search", "script_run"))
def test_agent_dispatch_refuses_tools_outside_the_user_allowlist(
    monkeypatch, auto_mode, user_caller, tool,
):
    called = _record(monkeypatch, tool)
    with server._managed_agent_admission_scope():
        result = server._agent_dispatch(tool, {"path": "x", "content": "y", "code": "1"}, allow_web=True)
    assert result.startswith("ERROR: HOST POLICY"), result
    assert called == []


def test_agent_gate_does_not_apply_without_a_bound_restricted_role(monkeypatch, auto_mode):
    assert role_tool_policy.agent_refusal("file_write") == ""
    token = role_tool_policy._CALLER_ROLE.set(None)
    try:
        assert server._agent_permission_gate_error("file_write", {}, record=False) == ""
    finally:
        role_tool_policy._CALLER_ROLE.reset(token)


def test_agent_gate_refusal_precedes_the_permission_decision(monkeypatch, auto_mode, user_caller):
    monkeypatch.setattr(
        server.permission_modes, "decide",
        lambda *a, **k: pytest.fail("role gate must run before the permission decision"),
    )
    assert server._agent_permission_gate_error("run_code", {}).startswith("ERROR: HOST POLICY")


def test_computer_use_chat_route_is_refused_by_the_agent_gate_for_user(
    monkeypatch, auto_mode, user_caller,
):
    called = _record(monkeypatch, "computer_use_stop", "computer_use_status", "computer_task")
    result = computer_use_chat.route("stop controlling", server)
    assert "HOST POLICY" in result
    assert called == []


# --- the web tool loop of a chat turn ---------------------------------------

from tests.test_role_tool_gate import chat_server  # noqa: E402,F401  (fixture reuse)


@pytest.mark.parametrize("role,expected", (("user", False), ("developer", True), ("admin", True)))
def test_chat_turn_web_tool_loop_runs_only_above_user(
    monkeypatch, chat_server, role, expected,  # noqa: F811 - imported pytest fixture
):
    import sonder_runtime.interfaces.http.serve as serve

    web_calls = []
    monkeypatch.setattr(serve._legacy_runtime(), "route_computer_use", lambda prompt: None)
    monkeypatch.setattr(
        serve._legacy_runtime(), "chat_web_response",
        lambda *a, **k: (web_calls.append(role), None)[1],
    )
    status, payload, _calls = chat_server(role, "what is the weather in Paris today")
    assert status == 200, payload
    assert bool(web_calls) is expected


# --- the request binds the caller role the agent gate reads ------------------

@pytest.mark.parametrize("role,expected", (
    ("user", "user"), ("developer", None), ("admin", None), ("local-open", None),
))
def test_request_auth_context_binds_the_caller_role(monkeypatch, role, expected):
    import contextvars
    import types

    import sonder_runtime.interfaces.http.serve as serve
    from tests.test_role_tool_gate import _context

    monkeypatch.setattr(serve, "_auth_context", lambda *_a, **_k: _context(role))
    handler = types.SimpleNamespace(headers={})

    def run():
        serve.Handler._request_auth_context(handler)
        return role_tool_policy._CALLER_ROLE.get()

    assert contextvars.copy_context().run(run) == expected
