import pytest

from sonder_runtime.domain.computer_use.intent import classify


@pytest.mark.parametrize(
    "text, tool",
    [
        ("control notepad and type hello", "computer_task"),
        ("open notepad and write a shopping list", "computer_task"),
        ("click the Save button in paint", "computer_task"),
        ("what's on my screen", "screen_capture"),
        ("use my computer to open calculator", "computer_task"),
        ("use my computer to open the file X", "computer_task"),
        ("control notepad and write code", "computer_task"),
        ("stop controlling", "computer_use_stop"),
        ("stop driving", "computer_use_stop"),
        ("Could you please click the Save button in paint?", "computer_task"),
        ("Please stop controlling my computer", "computer_use_stop"),
        ("what’s on my screen?", "screen_capture"),
    ],
)
def test_computer_use_phrases_route_to_gated_tools(text, tool):
    result = classify(text)
    assert result is not None
    assert result.tool == tool


@pytest.mark.parametrize(
    "text",
    [
        "type hints in foo.py",
        "click handler bug",
        "open the file X",
        "write a function to control the parser",
        "what is on my screen in the screenshot documentation?",
        "type a word",
        "computer is slow",
        "what screen sizes does edge support",
        "click handler bug in paint",
        "open README.md",
        "open notepad.py",
        "write a story about my computer",
        "explain how to use my computer to open notepad",
        "write about chrome",
    ],
)
def test_coding_and_file_requests_do_not_route_to_computer_use(text):
    assert classify(text) is None


def test_route_preserves_user_goal_for_task_tools():
    result = classify("control notepad and type hello")
    assert result.args == {"goal": "control notepad and type hello", "max_steps": 10}


def test_chat_route_uses_managed_gated_dispatch_in_status_first_order(monkeypatch):
    from contextlib import nullcontext
    import server
    route = server.route_computer_use

    calls = []

    def fake_dispatch(name, args, **kwargs):
        calls.append((name, args, kwargs))
        if name == "computer_use_status":
            return '{"ok":true,"enabled":true,"allowed_apps":["notepad.exe"],"session":{"active":true,"session":"s1"}}'
        return "routed"

    monkeypatch.setattr(server, "_agent_dispatch", fake_dispatch)
    monkeypatch.setattr(server, "_managed_agent_admission_scope", lambda: nullcontext())
    assert route("what's on my screen") == "routed"
    assert calls[0][0] == "computer_use_status"
    assert calls[1][0] == "screen_capture"
    assert calls[1][1] == {"question": "what's on my screen"}


def test_stop_is_fixed_dispatch_without_model(monkeypatch):
    from contextlib import nullcontext
    import server
    route = server.route_computer_use

    calls = []
    monkeypatch.setattr(server, "_agent_dispatch", lambda name, args, **kwargs: calls.append(name) or '{"ok":true}')
    monkeypatch.setattr(server, "_managed_agent_admission_scope", lambda: nullcontext())
    assert route("stop driving") == '{"ok":true}'
    assert calls == ["computer_use_stop"]


def test_disabled_computer_use_returns_concrete_guidance(monkeypatch):
    from contextlib import nullcontext
    import server
    route = server.route_computer_use

    monkeypatch.setattr(server, "_agent_dispatch", lambda name, args, **kwargs: '{"ok":true,"enabled":false}')
    monkeypatch.setattr(server, "_managed_agent_admission_scope", lambda: nullcontext())
    assert "enabled = true" in route("what's on my screen")


def test_action_gate_refusal_is_returned_without_fallback(monkeypatch):
    from contextlib import nullcontext
    import server
    route = server.route_computer_use

    def dispatch(name, _args, **_kwargs):
        if name == "computer_use_status":
            return '{"ok":true,"enabled":true,"allowed_apps":["notepad.exe"],"session":{"active":true,"session":"s1"}}'
        return "ERROR: HOST POLICY: tool 'computer_task' is refused"

    monkeypatch.setattr(server, "_agent_dispatch", dispatch)
    monkeypatch.setattr(server, "_managed_agent_admission_scope", lambda: nullcontext())
    assert "HOST POLICY" in route("control notepad and type hello")


@pytest.mark.parametrize("payload", [
    '{"ok":true,"enabled":true,"allowed_apps":[],"session":{"active":false}}',
    '{"ok":true,"enabled":false,"allowed_apps":[],"session":{"active":false}}',
])
def test_status_guidance_requires_enabled_and_allowlist(payload):
    from sonder_runtime.bootstrap.computer_use_chat import _status_guidance

    assert "allowed_apps" in _status_guidance(payload)


def test_status_guidance_matches_real_session_status_contract():
    from sonder_runtime.bootstrap.computer_use_chat import _status_guidance

    assert _status_guidance(
        '{"ok":true,"enabled":true,"allowed_apps":["notepad.exe"],'
        '"session":{"active":true,"session":"s1","app":"notepad.exe"}}'
    ) == ""
    result = _status_guidance(
        '{"ok":true,"enabled":true,"allowed_apps":["notepad.exe"],"session":{"active":false}}'
    )
    assert "computer_use_start" in result
    assert "/approve" in result


@pytest.mark.parametrize("payload", ['[]', 'null', '{"ok":false,"error":"unavailable"}', 'ERROR: gated'])
def test_status_failure_does_not_become_an_action(payload):
    from sonder_runtime.bootstrap.computer_use_chat import _status_guidance

    assert _status_guidance(payload)


def test_inactive_session_dispatches_only_status(monkeypatch):
    import server
    route = server.route_computer_use

    calls = []
    def dispatch(name, args, **kwargs):
        calls.append(name)
        return '{"ok":true,"enabled":true,"allowed_apps":["notepad.exe"],"session":{"active":false}}'
    monkeypatch.setattr(server, "_agent_dispatch", dispatch)
    assert "computer_use_start" in route("control notepad and type hello")
    assert calls == ["computer_use_status"]


def test_run_refusal_prevents_even_status_dispatch(monkeypatch):
    import server
    route = server.route_computer_use

    monkeypatch.setattr(server, "_agent_run_tool_refusal", lambda *a, **k: "refused by this run")
    monkeypatch.setattr(server, "_agent_dispatch", lambda *a, **k: pytest.fail("refused call dispatched"))
    assert "HOST POLICY" in route("what's on my screen")


_CHAT_PHRASES = (
    "control notepad and type hello", "open notepad and write a shopping list",
    "click the Save button in paint", "what's on my screen",
    "use my computer to open calculator", "stop controlling", "stop driving",
)


def _fake_desktop_dispatch(monkeypatch):
    import server

    calls = []
    def dispatch(name, args, **kwargs):
        calls.append(name)
        if name == "computer_use_status":
            return '{"ok":true,"enabled":true,"allowed_apps":["notepad.exe"],"session":{"active":true,"session":"test"}}'
        return "DESKTOP ROUTED"
    monkeypatch.setattr(server, "_agent_dispatch", dispatch)
    monkeypatch.setattr(server, "_agent_run_tool_refusal", lambda *a, **k: "")
    return calls


def test_repl_main_routes_desktop_turns_without_workspace_or_chat(monkeypatch):
    import server
    from sonder_runtime.interfaces.repl import repl

    calls = _fake_desktop_dispatch(monkeypatch)
    monkeypatch.setattr(repl, "_legacy_runtime", None)
    repl.configure_legacy_runtime(server)
    lines = iter((*_CHAT_PHRASES, "/exit"))
    monkeypatch.setattr(repl, "_read_input", lambda *a, **k: next(lines, "/exit"))
    monkeypatch.setattr(repl, "_startup_banner", lambda *a: "")
    monkeypatch.setattr(repl, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(repl, "_named_command_gate", lambda *a: (True, ""))
    monkeypatch.setattr(server, "sonder", lambda *a, **k: pytest.fail("plain chat intercepted desktop turn"))
    monkeypatch.setattr(repl, "_run_session_work", lambda *a, **k: pytest.fail("workspace intercepted desktop turn"))
    replies = []
    monkeypatch.setattr(repl, "_print_chat_result", lambda text, *a, **k: replies.append(text))
    repl.main()
    assert replies == ["DESKTOP ROUTED"] * len(_CHAT_PHRASES)
    assert calls.count("computer_task") == 4
    assert calls.count("screen_capture") == 1
    assert calls.count("computer_use_stop") == 2


def test_http_chat_completions_routes_desktop_turns_before_work_and_chat(monkeypatch):
    import json
    import server
    from sonder_runtime.interfaces.http import serve
    from tests.test_serve_auth import _http_server, _request

    calls = _fake_desktop_dispatch(monkeypatch)
    monkeypatch.setattr(serve, "_LEGACY_RUNTIME", None)
    serve.configure_legacy_runtime(server)
    monkeypatch.setattr(serve, "API_KEY", "")
    monkeypatch.setattr(serve, "AUTH_MODE", "local-open")
    monkeypatch.setattr(serve, "REQUIRE_ACCOUNT", False)
    monkeypatch.setattr(server, "prewarm_model", lambda *a, **k: None)
    monkeypatch.setattr(server, "answer_with_history", lambda *a, **k: pytest.fail("plain chat intercepted desktop turn"))
    monkeypatch.setattr(serve, "_handle_work_intent", lambda *a, **k: pytest.fail("workspace intercepted desktop turn"))
    with _http_server(monkeypatch) as port:
        for text in _CHAT_PHRASES:
            status, _, body = _request(
                port, "POST", "/v1/chat/completions",
                body=json.dumps({"model": "sonder", "messages": [{"role": "user", "content": text}]}),
                headers={"Content-Type": "application/json"},
            )
            assert status == 200, body
            assert json.loads(body)["choices"][0]["message"]["content"] == "DESKTOP ROUTED"
    assert calls.count("computer_task") == 4
    assert calls.count("screen_capture") == 1
    assert calls.count("computer_use_stop") == 2
