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
    ("build_run", {"command": 'dotnet build pwsh.sln'}),
    ("build_run", {"command": r'type C:\PowerShell\notes.txt'}),
    ("build_run", {"command": 'npm run x -- --pwsh'}),
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


@pytest.mark.parametrize("command", [
    "cmd.exe /c powershell -enc AA==", "if 1==1 powershell -enc AA==", "(powershell -enc AA==)",
    "@powershell -enc AA==", "echo hi\npowershell -enc AA==", 'echo hi\r\npwsh "iex $x"',
    "start /b powershell -enc AA==", 'start "" powershell -enc AA==', 'if exist x pwsh "iex $x"',
    "for %f in (a) do pwsh x", "conhost powershell -enc AA==", "cmd/cpwsh x",
    "powershell -c Get-Date & cmd /c powershell -enc AA==",
    # G0896 cycle 2: any non-leading occurrence is opaque, including cmd.exe
    # spellings with redirection prefixes, quoted /c bodies and start options.
    '>nul powershell "[Environment]::Exit(7)"', '2>nul powershell "[Environment]::Exit(7)"',
    'cmd /c " powershell "[Environment]::Exit(7)""',
    r'start /d C:\Windows /wait powershell "[Environment]::Exit(7)"',
    'p^owershell -enc AA==', '"power"shell -enc AA==', 'powershell.exe. -enc AA==',
    'powershell -c Get-Date & "pw"sh -enc AA==', 'pwsh"x & y',
    # Mentions are opaque too; opaque only means approval is required.
    'echo powershell', 'git commit -m "pwsh support"',
])
def test_build_run_launchers_outside_command_start_are_opaque(monkeypatch, command):
    def unexpected(source):
        pytest.fail("non-leading launcher must not be tokenized as PowerShell")
    monkeypatch.setattr(gate, "inspect_powershell", unexpected)
    assert gate.powershell_arguments("build_run", {"command": command}) is not None
    result = gate.inspect_tool_call("build_run", {"command": command})
    assert result is not None and not result.inspectable


@pytest.mark.parametrize("command", [
    "powershell -NoProfile -Command Get-Date", 'pwsh.exe -File build.ps1',
    r'"C:\Program Files\PowerShell\7\pwsh.exe" -c Get-Date',
])
def test_build_run_leading_launcher_is_inspected(monkeypatch, command):
    seen = []
    monkeypatch.setattr(gate, "inspect_powershell", lambda source: (
        seen.append(source) or SimpleNamespace(inspectable=True, reason="")))
    assert gate.inspect_tool_call("build_run", {"command": command}).inspectable
    assert seen == [command]


@pytest.mark.parametrize("command", [
    # Review repro: cmd.exe splices the expansion into the command word and
    # runs pwsh (G0896).
    "pw%PATHEXT:~0,0%sh -NoProfile -c echo viaexpansion", "pw%PATHEXT:~0,0%sh -enc AA==",
    "set X=pw&call %X%sh -enc AA==", "for %a in (pw) do @%ash -enc AA==",
    'cmd /v:on /c "set X=pw&& !X!sh -enc AA=="', "%COMSPEC% /c echo x", "echo %X%",
    "%ProgramFiles%\\PowerShell\\7\\pwsh.exe -enc AA==",
])
def test_build_run_cmd_expansion_is_opaque_before_name_matching(monkeypatch, command):
    def unexpected(source):
        pytest.fail("a command cmd.exe may rewrite must not be tokenized as PowerShell")
    monkeypatch.setattr(gate, "inspect_powershell", unexpected)
    assert gate.powershell_arguments("build_run", {"command": command}) is not None
    result = gate.inspect_tool_call("build_run", {"command": command})
    assert result is not None and not result.inspectable
    assert "expansion" in result.reason


@pytest.mark.parametrize("command", ["echo 100%", 'git commit -m "done!"', "make -j4 CFLAGS=-O2"])
def test_build_run_lone_percent_or_bang_is_not_an_expansion(monkeypatch, command):
    def unexpected(source):
        pytest.fail("unrelated build command must not start the parser")
    monkeypatch.setattr(gate, "inspect_powershell", unexpected)
    assert gate.powershell_arguments("build_run", {"command": command}) is None
    assert gate.inspect_tool_call("build_run", {"command": command}) is None


@pytest.mark.parametrize("command", [
    # start resolves the path through Win32 normalization, which drops the
    # trailing "." component; 8.3 short names name the same file.
    r"start /b /wait C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe\. -enc AA==",
    "start /b powers~1 -enc AA==", "echo x & POWERS~1.EXE -enc AA==",
])
def test_build_run_normalized_launcher_names_are_selected(monkeypatch, command):
    def unexpected(source):
        pytest.fail("non-leading launcher must not be tokenized as PowerShell")
    monkeypatch.setattr(gate, "inspect_powershell", unexpected)
    assert gate.powershell_arguments("build_run", {"command": command}) is not None
    result = gate.inspect_tool_call("build_run", {"command": command})
    assert result is not None and not result.inspectable


def test_build_run_leading_short_name_launcher_is_inspected(monkeypatch):
    seen = []
    monkeypatch.setattr(gate, "inspect_powershell", lambda source: (
        seen.append(source) or SimpleNamespace(inspectable=False, reason="encoded PowerShell command")))
    assert not gate.inspect_tool_call("build_run", {"command": "POWERS~1 -enc AA=="}).inspectable
    assert seen == ["POWERS~1 -enc AA=="]


@pytest.mark.parametrize("program", [
    "powershell.exe.", "powershell.exe ", " pwsh", "PWSH.EXE. ", "POWERS~1.EXE",
    r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe\.",
    r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe\. ", "C:/tools/pwsh.exe/.",
])
def test_argv_program_paths_are_normalized_before_matching(monkeypatch, program):
    seen = []
    monkeypatch.setattr(gate, "inspect_powershell", lambda source: (
        seen.append(source) or SimpleNamespace(inspectable=False, reason="encoded PowerShell command")))
    arguments = {"program": program, "args_json": json.dumps(["-enc", "AA=="])}
    assert gate.powershell_arguments("workspace_run", arguments) is not None
    assert not gate.inspect_tool_call("workspace_run", arguments).inspectable
    assert len(seen) == 1
    project = {"commands_json": json.dumps([{"cmd": [program, "-enc", "AA=="]}])}
    assert gate.powershell_arguments("run_project", project) is not None


@pytest.mark.parametrize("program", ["pwsh.sln", "python", r"C:\PowerShell\notes.txt", "pwsh-x"])
def test_argv_programs_that_are_not_powershell_stay_unselected(program):
    assert gate.powershell_arguments("workspace_run", {"program": program, "args_json": "[]"}) is None


@pytest.mark.parametrize("command", [
    "if " * 3000, "cmd/" * 5000, "a/" * 10000, "if x/" * 2000, '"a/' * 7000, "start /" * 3000,
    "pwsh\\" + "." * 50000 + "x", "pwsh\\." * 10000,
], ids=[f"linear_{i}" for i in range(8)])
def test_build_run_launcher_scan_is_linear(command):
    import time
    started = time.perf_counter()
    gate.inspect_tool_call("build_run", {"command": command})
    assert time.perf_counter() - started < 2.0


def test_build_run_backtick_quoting_around_powershell_is_opaque(monkeypatch):
    def unexpected(source):
        pytest.fail("backtick-escaped cmd.exe launcher must not be tokenized as PowerShell")
    monkeypatch.setattr(gate, "inspect_powershell", unexpected)
    command = 'pwsh -Command "Write-Output `"1 ; iex (Get-Date)`""'
    assert gate.powershell_arguments("build_run", {"command": command}) is not None
    result = gate.inspect_tool_call("build_run", {"command": command})
    assert result is not None and not result.inspectable


@pytest.mark.parametrize("tool", ["script_run", "run_script"])
@pytest.mark.parametrize("path", ["evil.ps1/", "evil.ps1.", r"evil.ps1\.", "evil.ps1 "])
def test_script_tools_normalize_ps1_paths_before_selection(tool, path):
    arguments = {"path": path}
    assert gate.powershell_arguments(tool, arguments) is not None
    result = gate.inspect_tool_call(tool, arguments)
    assert result is not None and not result.inspectable


@pytest.mark.parametrize("script_arg", ["evil.ps1.", "evil.ps1 ", r"C:\work\evil.ps1."])
def test_argv_normalized_positional_ps1_cannot_hide_behind_benign_stdin(monkeypatch, script_arg):
    def inspect(source):
        if source == "Get-Date":
            return SimpleNamespace(inspectable=True, reason="ast fully inspectable")
        return SimpleNamespace(inspectable=False, reason="external PowerShell script needs inspection")
    monkeypatch.setattr(gate, "inspect_powershell", inspect)
    args = {"program": "pwsh", "args_json": json.dumps([script_arg]), "stdin": "Get-Date"}
    result = gate.inspect_tool_call("workspace_run", args)
    assert result is not None and not result.inspectable

