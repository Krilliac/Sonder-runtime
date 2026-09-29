"""PowerShell argument propagation at the loop and console gates."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import permission_modes
import server
import sonder_runtime.interfaces.repl.repl as repl

pytestmark = pytest.mark.unit


def _decision(action="allow", risk="execution"):
    return SimpleNamespace(
        action=action,
        allowed=action == "allow",
        risk=risk,
        mode="manual",
        reason="test",
        source="test",
    )


def test_loop_gate_receives_powerShell_action_arguments(monkeypatch):
    seen = {}

    def decide(tool, **kwargs):
        seen.update(tool=tool, **kwargs)
        return _decision()

    monkeypatch.setattr(permission_modes, "decide", decide)
    action = {"type": "run_code", "language": "powershell", "code": "Get-ChildItem"}
    assert server._loop_permission_refusal("run_code", action) is None
    assert seen["arguments"] is action


def test_loop_gate_blocks_unsafe_powerShell_before_runner(monkeypatch):
    called = []

    def decide(tool, **kwargs):
        called.append((tool, kwargs["arguments"]))
        return _decision("deny", "dangerous")

    monkeypatch.setattr(permission_modes, "decide", decide)
    action = {"type": "run_code", "language": "powershell", "code": "Invoke-Expression $x"}
    refusal = server._loop_dispatch(action)
    assert refusal["ok"] is False
    assert called and called[0][1] is action


def test_loop_gate_allows_benign_powerShell_with_original_dispatch(monkeypatch):
    seen = {}

    def decide(tool, **kwargs):
        seen.update(tool=tool, **kwargs)
        return _decision()

    monkeypatch.setattr(permission_modes, "decide", decide)
    monkeypatch.setattr(server.code_runner, "run_code", lambda **kwargs: {
        "ok": True, "returncode": 0, "stdout": "ok", "stderr": "",
    })
    result = server._loop_dispatch({
        "type": "run_code", "language": "powershell", "code": "Get-ChildItem",
    })
    assert result["ok"] is True
    assert seen["arguments"]["code"] == "Get-ChildItem"


def test_console_catalog_gate_passes_parsed_arguments(monkeypatch):
    seen = {}

    def decide(tool, **kwargs):
        seen.update(tool=tool, **kwargs)
        return _decision()

    monkeypatch.setattr(repl.permission_policy, "decide_for_caller", decide)
    arguments = {"code": "Get-ChildItem", "language": "powershell"}
    assert repl._named_command_gate("/runwindow", "", arguments=arguments) == (True, "")
    assert seen["arguments"] is arguments


def test_console_runwindow_raises_unsafe_powerShell_to_gate(monkeypatch):
    seen = {}

    def decide(tool, **kwargs):
        seen.update(tool=tool, **kwargs)
        return _decision("deny", "dangerous")

    monkeypatch.setattr(repl.permission_policy, "decide_for_caller", decide)
    arguments = {"code": "Invoke-Expression $x", "language": "powershell"}
    allowed, refusal = repl._named_command_gate("/runwindow", "", arguments=arguments)
    assert allowed is False
    assert "refused" in refusal
    assert seen["arguments"] is arguments


def test_catalogued_dispatch_passes_arguments_before_handler(monkeypatch):
    seen = {}

    def decide(tool, **kwargs):
        seen.update(tool=tool, **kwargs)
        return _decision()

    monkeypatch.setattr(repl.permission_policy, "decide_for_caller", decide)
    called = []

    def handler(**kwargs):
        called.append(kwargs)
        return "benign"

    previous_runtime = repl._legacy_runtime
    monkeypatch.setattr(
        repl.command_catalog, "parse_invocation",
        lambda _line: ("workspace_run", {"program": "pwsh", "args_json": '["-File","ok.ps1"]'}),
    )
    repl.configure_legacy_runtime(server)
    monkeypatch.setattr(server, "workspace_run", handler)
    try:
        output = repl._run_catalogued("/workspace_run", "/workspace_run")
    finally:
        repl._legacy_runtime = previous_runtime
    assert output == "benign"
    assert called
    assert seen["arguments"]["program"] == "pwsh"


@pytest.mark.parametrize("inspectable,expected", [(False, "refused"), (True, "ran")])
def test_catalogued_powershell_uses_real_mode_decider(monkeypatch, inspectable, expected):
    from sonder_runtime.adapters.security import powershell_gate

    args = {"code": "Get-Date" if inspectable else "iex $code", "language": "powershell"}
    monkeypatch.setattr(powershell_gate, "inspect_powershell", lambda source: SimpleNamespace(
        inspectable=inspectable, reason="ast verdict"))
    monkeypatch.setattr(repl.command_catalog, "parse_invocation", lambda line: ("run_code", args))
    monkeypatch.setattr(repl, "_console_has_operator", lambda: False)

    def decide(tool, **kwargs):
        return permission_modes.decide(
            tool, mode=permission_modes.AUTO, interactive=False, record=False,
            arguments=kwargs["arguments"], rule_lookup=lambda name: None,
        )

    monkeypatch.setattr(repl.permission_policy, "decide_for_caller", decide)
    called = []
    monkeypatch.setattr(server, "run_code", lambda **kwargs: called.append(kwargs) or "ran")
    monkeypatch.setattr(repl, "_legacy_runtime", server)
    output = repl._run_catalogued("/run_code", "/run_code")
    assert expected in output
    assert bool(called) is inspectable


@pytest.mark.parametrize("surface", ["http-run", "http-window", "http-intent", "control-run"])
@pytest.mark.parametrize("inspectable", [False, True])
def test_message_code_is_bound_at_every_surface(monkeypatch, surface, inspectable):
    import sonder_runtime.interfaces.http.serve as serve
    from sonder_runtime.adapters.security import powershell_gate

    monkeypatch.setattr(serve, "_LEGACY_RUNTIME", server)
    messages = [{"role": "assistant", "content": "```powershell\nGet-Date\n```"}]
    monkeypatch.setattr(powershell_gate, "inspect_powershell", lambda source: SimpleNamespace(
        inspectable=inspectable, reason="test AST verdict"))

    def decide(tool, **kwargs):
        return permission_modes.decide(
            tool, mode=permission_modes.AUTO, interactive=False, record=False,
            arguments=kwargs.get("arguments"), rule_lookup=lambda name: None,
        )

    monkeypatch.setattr(serve.permission_policy, "decide_for_caller", decide)
    called = []

    def runner(*args, **kwargs):
        called.append((args, kwargs))
        return {"ok": True, "returncode": 0, "stdout": "ok", "stderr": "", "language": "powershell"}

    monkeypatch.setattr(serve.code_runner, "run_code", runner)
    monkeypatch.setattr(serve.code_runner, "run_code_window", runner)
    monkeypatch.setattr(serve.code_runner, "format_result", lambda result: "ran")
    monkeypatch.setattr(serve.code_runner, "format_window_result", lambda result: "ran")
    if surface == "http-intent":
        monkeypatch.setattr(serve.intents, "classify", lambda value: {"run": True})
        output = serve._handle_intent("run it", messages=messages)
    elif surface == "control-run":
        output = server.control_command("/run", history=messages)
    else:
        output = serve._handle_slash("/runwindow" if surface == "http-window" else "/run", messages=messages)
    assert bool(called) is inspectable, output
    assert ("refused" in output) is not inspectable


def test_legacy_non_powershell_intent_is_unchanged():
    from sonder_runtime.adapters.security.powershell_gate import run_intent
    assert run_intent(lambda: "existing", lambda *a: pytest.fail("unexpected gate"),
                      ["source"], lambda source: {"code": "print(1)", "language": "python"}) == "existing"


# --- non-PowerShell calls keep origin/main's argument-free decisions --------


@pytest.mark.parametrize("action", [
    {"type": "run_code", "language": "python", "code": "print(1)"},
    {"type": "run_code", "code": "print(1)"},
    {"type": "workspace_run", "program": "git", "args": ["status"]},
    {"type": "file_write", "path": "a.txt", "content": "x"},
])
def test_loop_gate_keeps_no_arguments_for_other_actions(monkeypatch, action):
    seen = {}

    def decide(tool, **kwargs):
        seen.update(tool=tool, **kwargs)
        return _decision()

    monkeypatch.setattr(permission_modes, "decide", decide)
    server._loop_permission_refusal(action["type"], action)
    assert seen.get("arguments", None) is None


@pytest.mark.parametrize("tool,kwargs", [
    ("run_code", {"code": "print(1)", "language": "python"}),
    ("workspace_run", {"program": "git", "args_json": '["status"]'}),
    ("file_write", {"path": "a.txt", "content": "x"}),
])
def test_console_catalogue_keeps_no_arguments_for_other_calls(monkeypatch, tool, kwargs):
    seen = {}

    def decide(name, **options):
        seen.update(tool=name, **options)
        return _decision()

    monkeypatch.setattr(repl.permission_policy, "decide_for_caller", decide)
    monkeypatch.setattr(repl.command_catalog, "parse_invocation", lambda _line: (tool, dict(kwargs)))
    monkeypatch.setattr(server, tool, lambda **_kw: "ran")
    monkeypatch.setattr(repl, "_legacy_runtime", server)
    assert repl._run_catalogued("/" + tool, "/" + tool) == "ran"
    assert seen["arguments"] is None


@pytest.mark.parametrize("surface", ["http-run", "control-run"])
def test_python_run_blocks_keep_no_gate_arguments(monkeypatch, surface):
    import sonder_runtime.interfaces.http.serve as serve

    monkeypatch.setattr(serve, "_LEGACY_RUNTIME", server)
    messages = [{"role": "assistant", "content": "```python\nprint(1)\n```"}]
    seen = []

    def decide(tool, **kwargs):
        seen.append((tool, kwargs.get("arguments")))
        return _decision()

    monkeypatch.setattr(serve.permission_policy, "decide_for_caller", decide)
    monkeypatch.setattr(server.permission_modes, "decide_for_caller", decide)
    monkeypatch.setattr(server, "_control_run", lambda *a, **k: "ran")
    runner = lambda *a, **k: {"ok": True, "returncode": 0, "stdout": "ok", "stderr": "", "language": "python"}
    monkeypatch.setattr(serve.code_runner, "run_code", runner)
    monkeypatch.setattr(serve.code_runner, "format_result", lambda result: "ran")
    if surface == "control-run":
        server.control_command("/run", history=messages)
    else:
        serve._handle_slash("/run", messages=messages)
    assert seen and all(arguments is None for _tool, arguments in seen)


def test_powershell_selection_never_launches_the_parser(monkeypatch):
    from sonder_runtime.adapters.security import powershell_gate

    monkeypatch.setattr(powershell_gate, "inspect_powershell",
                        lambda source: pytest.fail("selection parsed source"))
    ps = {"code": "Get-Date", "language": "powershell"}
    assert powershell_gate.powershell_arguments("run_code", ps) is ps
    assert powershell_gate.powershell_arguments("run_code", {"code": "x"}) is None
    assert powershell_gate.powershell_arguments("file_write", ps) is None
    assert powershell_gate.slash_run_arguments("/help", lambda: pytest.fail("walked history"), None) is None
