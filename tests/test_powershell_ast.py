from pathlib import Path
from unittest.mock import patch
import subprocess
import sys
import pytest

from sonder_runtime.adapters.security.powershell_ast import PowerShellInspection, inspect_powershell, inspect_powershell_argv
from sonder_runtime.adapters.security import powershell_ast

requires_powershell = pytest.mark.skipif(
    powershell_ast._powershell_executable() is None, reason="native PowerShell parser is not installed",
)


@requires_powershell
def test_benign_command_is_inspectable():
    result = inspect_powershell("Get-ChildItem -LiteralPath 'x'")
    assert result.inspectable


@requires_powershell
def test_unsafe_constructs_raise():
    for source in (
        "pwsh -EncodedCommand QQ==", "iex $x", "& $command", ". $script", "Add-Type 'x'",
        "Start-Process $file", "Invoke-Command -ScriptBlock $sb", "$x.Invoke()",
        "pwsh -Command $payload", "pwsh -Command 'iex $x'",
    ):
        assert not inspect_powershell(source), source


def test_parser_failure_fails_closed_without_execution():
    with patch("sonder_runtime.adapters.security.powershell_ast._powershell_executable", return_value=None):
        result = inspect_powershell("Get-ChildItem")
    assert result == PowerShellInspection(False, "PowerShell parser unavailable")


@requires_powershell
def test_canary_is_not_created_by_classification():
    canary = Path(__file__).with_name(".powershell_ast_canary_7f39.tmp")
    try:
        canary.unlink(missing_ok=True)
        result = inspect_powershell(f"New-Item -Path '{canary}'")
        assert result.inspectable, result.reason
        assert not canary.exists()
    finally:
        canary.unlink(missing_ok=True)


@requires_powershell
def test_static_methods_and_argv_flags_are_preserved():
    assert inspect_powershell("[Math]::Abs(-2) | Out-Null")
    assert not inspect_powershell_argv(["pwsh", "-EncodedCommand", "QQ=="])
    assert inspect_powershell_argv(["pwsh", "-Command", "Get-ChildItem"])


@requires_powershell
def test_dynamic_members_and_nested_invocation_are_rejected():
    for source in (
        "$ExecutionContext.InvokeCommand.InvokeScript($x)", "$x.$member()",
        "[scriptblock]::Create($x)",
        "Invoke-Command $sb", "Start-Process pwsh -ArgumentList '-Command','iex $x'",
        "pwsh -co $payload", "pwsh -Command 'Get-Date' $opaque",
    ):
        assert not inspect_powershell(source), source


def test_timeout_and_protocol_fail_closed(monkeypatch):
    monkeypatch.setattr(powershell_ast, "_powershell_executable", lambda: sys.executable)
    completed = subprocess.CompletedProcess([], 0, stdout='{"inspectable":"false","reason":"bad"}', stderr="")
    with patch("sonder_runtime.adapters.security.powershell_ast.subprocess.run", return_value=completed):
        assert not inspect_powershell("Get-Date")
    with patch("sonder_runtime.adapters.security.powershell_ast.subprocess.run", side_effect=subprocess.TimeoutExpired([], 3)):
        assert not inspect_powershell("Get-Date")


@requires_powershell
@pytest.mark.parametrize("source", [
    "pwsh -e AA==", "pwsh -ec AA==", "pwsh -en AA==", "pwsh -enc AA==",
    "pwsh -EncodedArguments AA==", "& 'pwsh' '-EncodedCommand' 'AA=='",
    "pwsh \u2013enc AA==", "C:/Windows/pwsh.exe -e AA==",
    "Microsoft.PowerShell.Utility\\Invoke-Expression $code", "i`ex $code",
    "& ('Get-' + $verb)", ". $file", "& $block",
    "$ExecutionContext.InvokeCommand", "[System.Management.Automation.ScriptBlock]::Create('Get-Date')",
    "$block.InvokeReturnAsIs()", "[scriptblock]$text",
    "Invoke-Command -ScriptBlock:$block", "Invoke-Command @parameters",
    "Start-Process -FilePath:$file", "Start-Process -fi $file", "saps $file",
    "Start-Process pwsh -ArgumentList '-enc AA=='", "Start-Process pwsh '-e AA=='",
    "Start-Process pwsh -ArgumentList '-NoProfile','-EncodedCommand','QQ=='",
    "Start-Process pwsh -ArgumentList '-NoProfile','-Command','iex $x'",
    "Start-Process pwsh -Args '-Command iex $code'",
    "Add-Type -TypeDefinition $source", "Get-ChildItem 'unterminated",
    "pwsh -co $code", "pwsh '-Command' 'iex $code'", "pwsh -f $file",
    "pwsh -File 'unseen.ps1'", ". 'unseen.ps1'", "pwsh --% -enc AA==",
    "Set-Alias run iex; run $code", "New-Item Function:run -Value 'iex $code'; run",
])
def test_required_opaque_constructs_live(source):
    result = inspect_powershell(source)
    assert not result.inspectable, source
    assert "failed closed" not in result.reason, result.reason


@requires_powershell
@pytest.mark.parametrize("source", [
    "Get-ChildItem", "Test-Path .", "git status --short", "python -V",
    "[Math]::Abs(-2)", "[IO.Path]::Combine('a', 'b')", "& { Get-Date }",
    "& 'git' 'status'", "Get-ChildItem $path | Select-Object Name",
    "Write-Output 'iex $code'", "Invoke-Command -ScriptBlock { Get-Date }",
    "Invoke-Command -Session $session -ScriptBlock { Get-Date }",
    "pwsh -NoProfile -Command 'Get-Date'", "pwsh -ExecutionPolicy Restricted -Command 'Get-Date'",
    "Start-Process git -ArgumentList $arguments",
    "Start-Process pwsh -ArgumentList '-Command', 'Get-Date'",
    "Start-Process pwsh -ArgumentList @('-Command', 'Get-Date')", "pwsh --version",
])
def test_benign_constructs_live(source):
    result = inspect_powershell(source)
    assert result.inspectable, result.reason


@pytest.mark.parametrize("stdout,code", [
    ('{"inspectable":true,"reason":"ok"}', 1),
    ('[]', 0), ('null', 0), ('not json', 0),
])
def test_bad_helper_responses_never_allow(stdout, code, monkeypatch):
    monkeypatch.setattr(powershell_ast, "_powershell_executable", lambda: sys.executable)
    completed = subprocess.CompletedProcess([], code, stdout=stdout, stderr="")
    with patch("sonder_runtime.adapters.security.powershell_ast.subprocess.run", return_value=completed):
        assert not inspect_powershell("Get-Date # uncached protocol response").inspectable
