"""Regression tests for Wave 2 execution-sandbox, PowerShell gate, harness, code_check, and workbench hardening."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import types
import importlib.machinery
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import harness_tools
import sonder_runtime.adapters.code_check as code_check
import sonder_runtime.adapters.execution_tools.code_runner as code_runner
import sonder_runtime.adapters.filesystem.workbench as workbench
import sonder_runtime.adapters.security.powershell_ast as powershell_ast
import sonder_runtime.adapters.security.powershell_gate as powershell_gate
import sonder_runtime.domain.agents.interpreter_argv as interpreter_argv

requires_powershell = pytest.mark.skipif(
    powershell_ast._powershell_executable() is None,
    reason="native PowerShell parser is not installed",
)


# ---------------------------------------------------------------------------
# 1. pwsh -i bypass & powershell_ast hardening
# ---------------------------------------------------------------------------

@requires_powershell
@pytest.mark.parametrize("source", [
    "pwsh -i '-ec' 'AA=='",
    "pwsh -i -ec AA==",
    "pwsh -i -e AA==",
    "pwsh -interactive '-ec' 'AA=='",
    "pwsh -inp '-ec' 'AA=='",
    "pwsh -WindowStyle '-ec' 'AA=='",
    "pwsh -ExecutionPolicy '-c' 'iex 1'",
    "pwsh -ep '-ec' 'AA=='",
    "pwsh -w -c 'iex 1'",
    "powershell.exe -i '-ec' 'AA=='",
    "powershell.exe -ep:Bypass -Command 'Get-Date'",
    "powershell.exe -file:x; Write-Output OK",
    "pwsh --encodedcommand AA==",
])
def test_powershell_ast_blocks_launcher_option_bypasses(source):
    verdict = powershell_ast.inspect_powershell(source)
    assert not verdict.inspectable, source
    assert "failed closed" not in verdict.reason, verdict.reason


@requires_powershell
@pytest.mark.parametrize("source", [
    "pwsh -i -Command 'Get-Date'",
    "pwsh -inp Text -Command 'Get-Date'",
    "pwsh -if Text -Command 'Get-Date'",
    "pwsh -ep Restricted -Command 'Get-Date'",
])
def test_powershell_ast_keeps_valid_launcher_options_inspectable(source):
    verdict = powershell_ast.inspect_powershell(source)
    assert verdict.inspectable, verdict.reason


def test_powershell_ast_uses_static_file_helper_and_replace_errors(monkeypatch):
    monkeypatch.setattr(powershell_ast, "_powershell_executable", lambda: sys.executable)
    completed = subprocess.CompletedProcess([], 0, stdout='{"inspectable":false,"reason":"ok"}', stderr="")
    with patch("sonder_runtime.adapters.security.powershell_ast.subprocess.run", return_value=completed) as run:
        powershell_ast.inspect_powershell("Get-Date # static helper check")
    argv = run.call_args.args[0]
    assert "-File" in argv
    assert argv[-1] == str(powershell_ast._HELPER_PATH)
    assert run.call_args.kwargs.get("errors") == "replace"


# ---------------------------------------------------------------------------
# 2. powershell.exe 5.1 ':' / '=' switch bypass in workbench
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("args", [
    ["-ep:Bypass; Write-Output OK", "-File", "a.ps1"],
    ["-file:x; Write-Output OK"],
    ["-ep=Bypass; Write-Output OK", "-File", "a.ps1"],
    ["-file=a.ps1"],
    ["-WindowStyle:Hidden", "-File", "a.ps1"],
    ["-NoProfile:true", "-File", "a.ps1"],
    ["-Version:5.1", "-File", "a.ps1"],
])
def test_workbench_powershell_refuses_colon_and_equals_switch_values(args):
    refusal = workbench._powershell_inline_refusal(args)
    assert refusal is not None


def test_workbench_powershell_still_allows_space_separated_file_invocation():
    assert workbench._powershell_inline_refusal(
        ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "a.ps1"]
    ) is None


# ---------------------------------------------------------------------------
# 3. Positional .ps1 + trailing tokens & duplicate/case-colliding run_project
# ---------------------------------------------------------------------------

@requires_powershell
def test_positional_ps1_with_trailing_tokens_fails_closed_live():
    verdict = powershell_ast.inspect_powershell("& 'powershell.exe' 'safe.ps1' ';' 'iex' '1+1'")
    assert not verdict.inspectable
    assert "trailing" in verdict.reason


def test_positional_ps1_with_trailing_tokens_fails_closed_at_gate(monkeypatch):
    monkeypatch.setattr(
        powershell_gate,
        "inspect_powershell",
        lambda source: SimpleNamespace(
            inspectable=(source == "Write-Host ok"),
            reason="ast fully inspectable" if source == "Write-Host ok" else "external PowerShell script needs inspection",
        ),
    )
    verdict = powershell_gate.inspect_tool_call(
        "run_project",
        {
            "files_json": [{"path": "safe.ps1", "content": "Write-Host ok"}],
            "commands_json": [["powershell.exe", "safe.ps1", ";", "iex", "1+1"]],
        },
    )
    assert verdict is not None and not verdict.inspectable


@pytest.mark.parametrize("files", [
    [
        {"path": "safe.ps1", "content": "Write-Host ok"},
        {"path": "SAFE.ps1", "content": "Invoke-Expression 1"},
    ],
    [
        {"path": "safe.ps1", "content": "Write-Host ok"},
        {"path": "./safe.ps1", "content": "Invoke-Expression 1"},
    ],
    [
        {"path": "sub/safe.ps1", "content": "Write-Host ok"},
        {"path": "SUB\\SAFE.PS1", "content": "Invoke-Expression 1"},
    ],
])
def test_run_project_rejects_duplicate_and_case_colliding_files(files):
    with pytest.raises(ValueError, match="duplicate or case-colliding"):
        code_runner._project_files_from_json(files)


def test_powershell_gate_fails_closed_on_case_colliding_run_project_files():
    verdict = powershell_gate.inspect_tool_call(
        "run_project",
        {
            "files_json": [
                {"path": "safe.ps1", "content": "Write-Host ok"},
                {"path": "SAFE.ps1", "content": "Invoke-Expression 1"},
            ],
            "commands_json": [["powershell.exe", "-File", "safe.ps1"]],
        },
    )
    assert verdict is not None and not verdict.inspectable


def test_powershell_gate_read_project_inspects_last_case_normalized_entry(monkeypatch):
    seen = []

    def fake_inspect(source):
        seen.append(source)
        if source.startswith("& "):
            return SimpleNamespace(inspectable=False, reason="external PowerShell script needs inspection")
        return SimpleNamespace(inspectable=("Invoke-Expression" not in source), reason="checked")

    monkeypatch.setattr(powershell_gate, "inspect_powershell", fake_inspect)
    monkeypatch.setattr(
        code_runner,
        "_project_files_from_json",
        lambda _raw: [
            {"path": "safe.ps1", "content": "Write-Host ok"},
            {"path": "SAFE.ps1", "content": "Invoke-Expression 1"},
        ],
    )
    verdict = powershell_gate.inspect_tool_call(
        "run_project",
        {
            "files_json": "ignored",
            "commands_json": [["powershell.exe", "-File", "safe.ps1"]],
        },
    )
    assert verdict is not None and not verdict.inspectable
    assert "Invoke-Expression 1" in seen


# ---------------------------------------------------------------------------
# 4. Windows path / 8.3 / ADS executable normalization & build_run Python -m
# ---------------------------------------------------------------------------

@requires_powershell
@pytest.mark.parametrize("source", [
    "Start-Process 'C:powershell.exe' '-ec AA=='",
    "Start-Process 'powershell.exe::$DATA' '-ec AA=='",
    "Start-Process 'C:pwsh.exe::$DATA' '-ec AA=='",
    "Start-Process 'powershell_ise.exe' '-ec AA=='",
    "& 'C:evil.ps1'",
    "& 'evil.ps1::$DATA'",
])
def test_powershell_ast_normalizes_drive_relative_ads_and_ise(source):
    verdict = powershell_ast.inspect_powershell(source)
    assert not verdict.inspectable, source
    assert "failed closed" not in verdict.reason, verdict.reason


@pytest.mark.parametrize("program", [
    "C:powershell.exe",
    "powershell.exe::$DATA",
    "C:powershell.exe::$DATA",
    "powershell_ise.exe",
    "powershell_ise",
    "POWERS~1.EXE",
    "C:POWERS~1.EXE::$DATA",
])
def test_powershell_gate_normalizes_drive_ads_ise_and_83(program):
    assert powershell_gate._is_powershell(program)
    assert powershell_gate.powershell_arguments(
        "workspace_run", {"program": program, "args_json": '["-enc", "AA=="]'}
    ) is not None


@pytest.mark.parametrize("script_path", ["C:evil.ps1", "evil.ps1::$DATA", "C:evil.ps1::$DATA"])
def test_powershell_gate_normalizes_ps1_drive_and_ads(script_path):
    assert powershell_gate._is_ps1_path(script_path)


@pytest.mark.parametrize("exe", ["POWERS~1.EXE", "C:POWERS~1.EXE::$DATA", "powershell_ise.exe"])
def test_workbench_program_guards_cover_83_short_names_and_ise(exe):
    with pytest.raises(PermissionError):
        workbench._check_program_guards(exe, ["-c", "Write-Output 1"])


def test_workbench_resolve_program_rejects_drive_relative_before_which(monkeypatch):
    monkeypatch.setattr(workbench.shutil, "which", lambda _name: "C:/Windows/System32/cmd.exe")
    with pytest.raises(PermissionError, match="drive-relative"):
        workbench._resolve_program("C:foo.exe")


@pytest.mark.parametrize("program", ["POWERS~1.EXE", "powers~2", "C:POWERS~1.EXE::$DATA", "powershell_ise.exe"])
def test_interpreter_argv_recognizes_powershell_83_and_ise(program):
    assert interpreter_argv.interpreter_family(interpreter_argv.program_name(program)) == "powershell"


@pytest.mark.parametrize("command", [
    "python -m runpy timeit x",
    "python -m cProfile -m timeit x",
    "python -m profile src/app.py",
    "python -m trace --trace src/app.py",
    "python -m ipython",
    "python -m idlelib",
    "python -mCPROFILE src/app.py",
])
def test_build_run_closes_python_dash_m_passthrough_modules(command):
    plan = interpreter_argv.analyze_build_command(command, stdin_present=False, is_windows=True)
    assert plan.inline_violation is True, command


# ---------------------------------------------------------------------------
# 5. harness_tools.py flag/arg injection & process tree / bounded output
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad_path", ["--fix", "--write", "--override-ini=x=1", "-p=evil", "./--fix"])
def test_harness_resolve_target_path_rejects_leading_dash(tmp_path, bad_path):
    with pytest.raises(ValueError, match="must not start with '-'"):
        harness_tools._resolve_target_path(tmp_path, bad_path)


def test_harness_lint_format_test_typecheck_block_flag_injection(tmp_path, monkeypatch):
    monkeypatch.setattr(harness_tools, "_run", lambda *a, **k: pytest.fail("should not execute _run"))
    with harness_tools.authorized_root_scope(tmp_path):
        with pytest.raises(ValueError):
            harness_tools.lint_run(root=str(tmp_path), tool="ruff", path="--fix", fix=False)
        with pytest.raises(ValueError):
            harness_tools.format_code(root=str(tmp_path), tool="prettier", path="--write", check_only=True)
        with pytest.raises(ValueError):
            harness_tools.test_run(root=str(tmp_path), framework="pytest", path="--override-ini=addopts=-s")
        with pytest.raises(ValueError):
            harness_tools.typecheck_run(root=str(tmp_path), tool="mypy", path="--python-executable=evil")


def test_harness_git_mutations_reject_leading_dash_refs(tmp_path, monkeypatch):
    monkeypatch.setattr(harness_tools, "_run_git", lambda *a, **k: pytest.fail("should not invoke git"))
    with harness_tools.authorized_root_scope(tmp_path):
        assert not harness_tools.git_branch(root=str(tmp_path), name="-D", base="main", checkout=False)["ok"]
        assert not harness_tools.git_branch(root=str(tmp_path), name="ok", base="--force", checkout=False)["ok"]
        assert not harness_tools.git_checkout(root=str(tmp_path), ref="--force")["ok"]
        assert not harness_tools.git_tag(root=str(tmp_path), name="-d")["ok"]
        assert not harness_tools.git_merge(root=str(tmp_path), branch="--abort")["ok"]
        assert not harness_tools.git_cherry_pick(root=str(tmp_path), commits_json='["--abort"]')["ok"]


@pytest.mark.parametrize("bad_packages", [
    '["--index-url=http://evil.invalid"]',
    '["--target=../../outside"]',
    '["-r", "requirements.txt"]',
    '["http://evil.invalid/pkg.whl"]',
    '["git+https://evil.invalid/repo.git"]',
    '["../../outside"]',
    '[123]',
    '["requests --extra"]',
])
def test_harness_dependency_tools_reject_unsafe_packages(tmp_path, monkeypatch, bad_packages):
    monkeypatch.setattr(harness_tools, "_run", lambda *a, **k: pytest.fail("should not execute _run"))
    with harness_tools.authorized_root_scope(tmp_path):
        assert not harness_tools.dependency_add(root=str(tmp_path), packages_json=bad_packages)["ok"]
        assert not harness_tools.dependency_remove(root=str(tmp_path), packages_json=bad_packages)["ok"]
        assert not harness_tools.dependency_update(root=str(tmp_path), packages_json=bad_packages)["ok"]


def test_harness_dependency_tools_enforce_live_refusal(tmp_path, monkeypatch):
    monkeypatch.setattr(harness_tools, "_live_refusal", lambda op, _root: {"ok": False, "error": f"live refusal: {op}"})
    monkeypatch.setattr(harness_tools, "_run", lambda *a, **k: pytest.fail("should not execute _run"))
    with harness_tools.authorized_root_scope(tmp_path):
        res = harness_tools.dependency_add(root=str(tmp_path), packages_json='["requests"]')
        assert not res["ok"] and "live refusal" in res["error"]


def test_harness_run_bounds_verbose_output_and_cleans_up_on_timeout(tmp_path):
    # Verbose output exceeds MAX_OUTPUT; result stays bounded and trimmed.
    verbose = harness_tools._run(
        [sys.executable, "-c", "print('A' * 400000)"],
        cwd=tmp_path,
        timeout=10,
    )
    assert verbose["ok"] is True
    assert "trimmed" in verbose["stdout"]
    assert len(verbose["stdout"]) < harness_tools.MAX_OUTPUT + 100

    # Timeout tears down child process cleanly and reports timed_out=True.
    timed = harness_tools._run(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        cwd=tmp_path,
        timeout=0.3,
    )
    assert timed["ok"] is False
    assert timed["timed_out"] is True
    assert timed["returncode"] == -1


# ---------------------------------------------------------------------------
# 6. code_runner.py & code_check.py reliability and security
# ---------------------------------------------------------------------------

def test_code_runner_c_compiler_calls_find_visual_studio_vcvars_and_uses_c11(monkeypatch, tmp_path):
    monkeypatch.setattr(code_runner.shutil, "which", lambda _name: None)
    monkeypatch.setattr(code_runner.os, "name", "nt")
    monkeypatch.setattr(code_runner, "_find_visual_studio_vcvars", lambda: r"C:\VS\vcvars64.bat")
    name, compiler = code_runner._c_compiler()
    assert (name, compiler) == ("msvc-vcvars", r"C:\VS\vcvars64.bat")

    captured = {}

    def fake_run(cmd, cwd, stdin, timeout, language):
        bat_path = cmd[-1]
        captured["bat"] = Path(bat_path).read_text(encoding="utf-8")
        return {"ok": True, "returncode": 0, "stdout": "", "stderr": "", "language": language}

    monkeypatch.setattr(code_runner, "_run_process", fake_run)
    code_runner._msvc_compile_result(
        r"C:\VS\vcvars64.bat", [str(tmp_path / "main.c")], str(tmp_path / "snippet.exe"),
        str(tmp_path), 10, "c",
    )
    assert "/std:c11" in captured["bat"]
    assert "/std:c++17" not in captured["bat"]


def test_code_runner_powershell_includes_noninteractive():
    cmd = code_runner.SUPPORTED_LANGUAGES["powershell"]["cmd"]("script.ps1")
    assert "-NonInteractive" in cmd
    assert "-NoProfile" in cmd


def test_code_runner_bat_quote_escapes_percent_and_rejects_metachar_filenames(monkeypatch, tmp_path):
    assert code_runner._bat_quote("a%TEMP%b.cpp") == '"a%%TEMP%%b.cpp"'
    monkeypatch.setattr(code_runner, "_cpp_compiler", lambda: ("msvc-vcvars", r"C:\VS\vcvars64.bat"))
    with pytest.raises(ValueError, match="unsafe batch character"):
        code_runner._auto_project_commands(str(tmp_path), [{"path": "a%TEMP%b.cpp", "content": "int main(){}"}])


def test_code_runner_format_result_noninteractive_hints():
    ps_res = {
        "ok": False, "returncode": 1, "language": "powershell", "cwd": ".", "timeout": 10,
        "stdout": "", "stderr": "Windows PowerShell is in NonInteractive mode. Read and Prompt functionality is not available.",
    }
    assert "non-interactive" in code_runner.format_result(ps_res).lower()

    cs_res = {
        "ok": False, "returncode": 1, "language": "csharp", "cwd": ".", "timeout": 10,
        "stdout": "Enter your name:", "stderr": "Unhandled exception. System.FormatException: Input string was not in a correct format.",
    }
    assert "non-interactive" in code_runner.format_result(cs_res).lower()

    js_res = {
        "ok": False, "returncode": 1, "language": "javascript", "cwd": ".", "timeout": 10,
        "stdout": "Enter value:", "stderr": "Error [ERR_USE_AFTER_CLOSE]: readline was closed",
    }
    assert "non-interactive" in code_runner.format_result(js_res).lower()


def test_code_check_scrubs_secrets_decodes_bom_prefers_ruff_and_filters_footer(monkeypatch, tmp_path):
    # 1. UTF-8-BOM JSON and TOML decode cleanly
    bom_json = tmp_path / "bom.json"
    bom_json.write_bytes(b'\xef\xbb\xbf{"ok": true}\n')
    assert code_check.check_file(bom_json, project_root=tmp_path) == "file_check bom.json: none"

    bom_toml = tmp_path / "bom.toml"
    bom_toml.write_bytes(b'\xef\xbb\xbfname = "demo"\n')
    assert code_check.check_file(bom_toml, project_root=tmp_path) == "file_check bom.toml: none"

    # 2. Ruff preferred over pyflakes when both are installed, env scrubbed, footer filtered
    fake_pyflakes = types.ModuleType("pyflakes")
    fake_pyflakes.__spec__ = importlib.machinery.ModuleSpec("pyflakes", None)
    monkeypatch.setitem(sys.modules, "pyflakes", fake_pyflakes)
    monkeypatch.setattr(code_check, "_ruff_path", lambda: "ruff")
    monkeypatch.setenv("SONDER_API_KEY", "secret-should-not-leak")
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret-openrouter")
    monkeypatch.setenv("SONDER_FILE_BYPASS", "1")

    seen = {}

    def fake_run(command, **kwargs):
        seen["command"] = command
        seen["env"] = kwargs.get("env")
        seen["stdin"] = kwargs.get("stdin")
        out = (
            f"{tmp_path / 'sample.py'}:1:8: F401 [*] `os` imported but unused\n"
            "Found 1 error.\n"
            "[*] 1 fixable with the `--fix` option.\n"
        )
        return subprocess.CompletedProcess(command, 1, stdout=out, stderr="")

    monkeypatch.setattr(code_check.subprocess, "run", fake_run)
    sample = tmp_path / "sample.py"
    sample.write_text("import os\n", encoding="utf-8")
    report = code_check.check_file(sample, project_root=tmp_path)
    assert seen["command"][0] == "ruff"
    assert seen["stdin"] == subprocess.DEVNULL
    assert "SONDER_API_KEY" not in seen["env"]
    assert "OPENROUTER_API_KEY" not in seen["env"]
    assert "SONDER_FILE_BYPASS" not in seen["env"]
    assert "1 issue(s)" in report
    assert "Found 1 error" not in report
    assert "fixable with" not in report


# ---------------------------------------------------------------------------
# 7. workbench.py FS fixes (workspace_inventory & read_line_range)
# ---------------------------------------------------------------------------

def test_workbench_workspace_inventory_hides_credential_stores_even_when_include_hidden(monkeypatch, tmp_path):
    monkeypatch.setenv("SONDER_FILE_ROOTS", str(tmp_path))
    (tmp_path / "app.py").write_text("print(1)\n", encoding="utf-8")
    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir()
    (ssh_dir / "id_rsa").write_text("PRIVATE KEY DATA" * 100, encoding="utf-8")
    (tmp_path / ".env").write_text("SECRET=12345" * 100, encoding="utf-8")

    result = workbench.workspace_inventory(
        str(tmp_path),
        include_hidden=True,
        include_ignored=True,
    )
    relatives = [row["relative"] for row in result["largest_files"]]
    areas = [row["path"] for row in result["top_areas"]]
    assert "app.py" in relatives
    assert not any(".ssh" in r or "id_rsa" in r or ".env" in r for r in relatives)
    assert ".ssh" not in areas
    assert result["files"] == 1


def test_workbench_read_line_range_gives_actionable_directory_error(monkeypatch, tmp_path):
    monkeypatch.setenv("SONDER_FILE_ROOTS", str(tmp_path))
    sub = tmp_path / "mydir"
    sub.mkdir()
    with pytest.raises(ValueError, match="is a directory; use directory_tree or file_find"):
        workbench.read_line_range(str(sub))
