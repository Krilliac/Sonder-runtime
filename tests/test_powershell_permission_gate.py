"""Per-call PowerShell risk, without changing tool-level catalog contracts."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import permission_modes as pm
from sonder_runtime.adapters.security import powershell_gate as gate


@pytest.mark.parametrize("tool,args", [
    ("run_code", {"language": "powershell", "code": "iex $code"}),
    ("runwindow", {"language": "ps1", "code": "iex $code"}),
    ("workspace_run", {"program": "pwsh", "args_json": '["-enc", "AA=="]'}),
    ("isolated_run", {"argv_json": '["pwsh", "-enc", "AA=="]'}),
    ("parallel_run_code", {"jobs_json": json.dumps([{"language": "ps1", "code": "iex $code"}])}),
])
@pytest.mark.parametrize("mode", pm.MODES)
@pytest.mark.parametrize("interactive", [True, False])
def test_opaque_calls_raise_per_call_risk(monkeypatch, tool, args, mode, interactive):
    monkeypatch.setattr(gate, "inspect_powershell", lambda source: SimpleNamespace(
        inspectable=False, reason="dynamic invocation"))
    decision = pm.decide(tool, mode=mode, interactive=interactive, arguments=args,
                         rule_lookup=lambda name: None, record=False)
    assert decision.risk == "dangerous"
    assert decision.action == (pm.ASK if interactive and mode != pm.PLAN else pm.DENY)
    assert "PowerShell" in decision.reason


@pytest.mark.parametrize("tool,args", [
    ("run_code", {"language": "python", "code": "print('pwsh -enc')"}),
    ("run_code", {"code": "print(1)"}),
    ("workspace_run", {"program": "git", "args_json": '["status"]'}),
    ("isolated_run", {"argv_json": '["python", "-V"]'}),
    ("sonder", {"prompt": "explain Invoke-Expression"}),
    ("file_write", {"content": "iex $code"}),
    ("build_run", {"command": 'git commit -m "pwsh support"'}),
    ("build_run", {"command": 'echo powershell'}),
])
def test_unrelated_calls_never_start_parser(monkeypatch, tool, args):
    def unexpected(source):
        pytest.fail("unrelated call started PowerShell")
    monkeypatch.setattr(gate, "inspect_powershell", unexpected)
    assert gate.inspect_tool_call(tool, args) is None
    assert gate.inspect_tool_call(tool, None) is None


def test_inspectable_keeps_original_decision(monkeypatch):
    monkeypatch.setattr(gate, "inspect_powershell", lambda source: SimpleNamespace(
        inspectable=True, reason=""))
    args = {"language": "powershell", "code": "Get-ChildItem"}
    for mode in pm.MODES:
        decision = pm.decide("run_code", mode=mode, arguments=args, record=False,
                             rule_lookup=lambda name: None)
        assert decision.risk == pm.risk_of("run_code") == "execution"
        assert decision.action == pm._MATRIX[mode]["execution"]


def test_allow_and_deny_rules_retain_existing_precedence(monkeypatch):
    monkeypatch.setattr(gate, "inspect_powershell", lambda source: SimpleNamespace(
        inspectable=False, reason="dynamic invocation"))
    args = {"language": "powershell", "code": "iex $code"}
    for rule, action in [("allow", pm.ALLOW), ("deny", pm.DENY)]:
        decision = pm.decide("run_code", mode=pm.AUTO, arguments=args,
                             interactive=False, record=False,
                             rule_lookup=lambda name, rule=rule: {"action": rule})
        assert decision.action == action
        assert decision.risk == "dangerous"


def test_parallel_inspects_code_and_checks(monkeypatch):
    sources = []
    monkeypatch.setattr(gate, "inspect_powershell", lambda source: (
        sources.append(source) or SimpleNamespace(inspectable=True, reason="")))
    gate.inspect_tool_call("parallel_run_code", {"jobs_json": json.dumps([
        {"lang": "ps1", "code": "Get-Date", "check": "iex $code"},
        {"language": "python", "code": "print(1)"},
    ])})
    assert sources == ["Get-Date\niex $code"]


def test_inspection_failure_never_lowers_unknown_risk(monkeypatch):
    monkeypatch.setattr(pm, "risk_of", lambda name: pm.UNCLASSIFIED)
    monkeypatch.setattr(gate, "inspect_powershell", lambda source: SimpleNamespace(
        inspectable=False, reason="parser unavailable"))
    decision = pm.decide("run_code", mode=pm.AUTO, arguments={
        "language": "powershell", "code": "Get-Date"}, interactive=False,
        record=False, rule_lookup=lambda name: None)
    assert decision.risk == pm.UNCLASSIFIED
    assert decision.action == pm.DENY


def test_project_inline_and_file_sources_are_inspected(monkeypatch):
    sources = []
    monkeypatch.setattr(gate, "inspect_powershell", lambda source: (
        sources.append(source) or SimpleNamespace(inspectable=True, reason="")))
    gate.inspect_tool_call("run_project", {
        "files_json": {"check.ps1": "Get-Date"},
        "commands_json": [["pwsh", "-File", "check.ps1"]],
    })
    assert "Get-Date" in sources
    sources.clear()
    gate.inspect_tool_call("run_project", {
        "commands_json": [["pwsh", "-Command", "iex $payload"]],
    })
    assert any("iex $payload" in source for source in sources)


@pytest.mark.parametrize("tool", ["parallel_generate_run_languages", "campaign_generate_compile_execute_record"])
def test_generated_powershell_defers_inspection_until_source_exists(tool):
    assert gate.inspect_tool_call(tool, {}) is None
    assert gate.inspect_tool_call(tool, {"languages": "powershell"}) is None
    assert gate.inspect_tool_call(tool, {"languages": "python,javascript"}) is None


def test_loop_alias_precedence_matches_executed_argv(monkeypatch):
    seen = []
    monkeypatch.setattr(gate, "inspect_powershell", lambda source: (
        seen.append(source) or SimpleNamespace(inspectable=False, reason="encoded command")))
    args = {"program": "pwsh", "args": ["-enc", "AA=="],
            "args_json": '["-Command", "Get-Date"]'}
    decision = pm.decide("workspace_run", arguments=args, surface="loop", mode=pm.AUTO,
                         record=False, rule_lookup=lambda name: None)
    assert decision.risk == "dangerous"
    assert "'-enc'" in seen[0]
    assert "Get-Date" not in seen[0]


def test_loop_project_alias_precedence_matches_execution():
    original = {"files": {"a.ps1": "iex $x"}, "files_json": {"a.ps1": "Get-Date"},
                "commands": [["pwsh", "-f", "a.ps1"]], "commands_json": [["python", "-V"]]}
    normalized = gate.loop_arguments(original)
    assert normalized["files_json"] == original["files"]
    assert normalized["commands_json"] == original["commands"]
