from __future__ import annotations

import importlib.machinery
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest

import sonder_runtime.adapters.code_check as code_check
from sonder_runtime.adapters.code_check import check_file
from sonder_runtime.bootstrap.code_check_agent_tools import post_edit_file_check


def _module_entry(name: str, installed: bool):
    """A ``sys.modules`` entry that answers both module probes for ``name``.

    ``None`` makes ``import name`` raise and ``importlib.util.find_spec(name)``
    return None; a module carrying a spec makes both succeed. Either way the
    machine's real install state is never consulted.
    """
    if not installed:
        return None
    module = types.ModuleType(name)
    module.__spec__ = importlib.machinery.ModuleSpec(name, None)
    return module


def _pin_detect_linter_probes(monkeypatch, *, ruff_on_path=False, ruff_module=False, pyflakes=False):
    """Pin every availability probe ``harness_tools._detect_linter`` makes.

    Ruff counts as available on PATH *or* as an importable module (``lint_run``
    then runs ``python -I -m ruff``), flake8 is looked up on PATH, and pyflakes
    is imported. CI installs ruff from requirements-dev.txt, which supplies both
    the executable and the module, so blocking PATH alone left ruff visible.
    """
    import harness_tools
    monkeypatch.setattr(harness_tools.shutil, "which",
                        lambda name: "/usr/bin/ruff" if ruff_on_path and name == "ruff" else None)
    monkeypatch.setitem(sys.modules, "ruff", _module_entry("ruff", ruff_module))
    monkeypatch.setitem(sys.modules, "pyflakes", _module_entry("pyflakes", pyflakes))


def _pin_file_check_linters(monkeypatch, *, pyflakes=False, ruff=None):
    """Pin the linters ``check_file`` can reach for a syntactically valid file.

    It asks ``importlib.util.find_spec("pyflakes")``, then ``_ruff_path()``
    (``SONDER_LINTER_PATH``, else PATH), and notes "no linter available" when
    neither answers, so unpinned the result depends on what is installed.
    """
    monkeypatch.setitem(sys.modules, "pyflakes", _module_entry("pyflakes", pyflakes))
    monkeypatch.setattr(code_check, "_ruff_path", lambda: ruff)


def test_python_syntax_error_is_reported(tmp_path: Path):
    path = tmp_path / "broken.py"
    path.write_text("x = 1\n\ndef f(:\n    pass\n", encoding="utf-8")
    output = check_file(path, project_root=tmp_path)
    assert "file_check" in output
    assert "SyntaxError" in output
    assert "3:" in output


@pytest.mark.parametrize(("pyflakes", "ruff", "ran", "report"), [
    (True, None, ["pyflakes"], "file_check clean.py: none"),
    (False, "ruff", ["ruff"], "file_check clean.py: none"),
    (False, None, [], "file_check clean.py: none\nnote: no linter available"),
], ids=["pyflakes", "ruff-only", "no-linter"])
def test_clean_python_is_bounded_and_clean(monkeypatch, tmp_path: Path, pyflakes, ruff, ran, report):
    _pin_file_check_linters(monkeypatch, pyflakes=pyflakes, ruff=ruff)
    checkers = []
    monkeypatch.setattr(code_check, "_external", lambda command, checker, deadline: checkers.append(checker) or [])
    path = tmp_path / "clean.py"
    path.write_text("value = 1\n", encoding="utf-8")
    output = check_file(path, project_root=tmp_path)
    assert len(output) <= 2000
    assert output == report
    assert checkers == ran


def test_cpp_is_explicitly_skipped(tmp_path: Path):
    path = tmp_path / "main.cpp"
    path.write_text("int main() { return 0; }\n", encoding="utf-8")
    assert "C/C++ checker skipped" in check_file(path, project_root=tmp_path)


def test_json_and_binary_are_checked_without_subprocess(tmp_path: Path):
    path = tmp_path / "bad.json"
    path.write_text("{oops", encoding="utf-8")
    assert "JSONDecodeError" in check_file(path, project_root=tmp_path)
    binary = tmp_path / "blob.py"
    binary.write_bytes(b"x\0y")
    assert "binary" in check_file(binary, project_root=tmp_path)


def test_max_items_is_bounded(tmp_path: Path):
    path = tmp_path / "bad.json"
    path.write_text("{oops", encoding="utf-8")
    assert "issue(s)" in check_file(path, 1, project_root=tmp_path)


def test_checker_reports_toml_yaml_and_cpp_without_installing(tmp_path: Path):
    toml = tmp_path / "bad.toml"
    toml.write_text("[broken\n", encoding="utf-8")
    assert "TOMLDecodeError" in check_file(toml, project_root=tmp_path)
    yaml = tmp_path / "config.yaml"
    yaml.write_text("key: value\n", encoding="utf-8")
    assert "yaml checker skipped" in check_file(yaml, project_root=tmp_path)
    cpp = tmp_path / "main.cpp"
    cpp.write_text("int main() { return 0; }\n", encoding="utf-8")
    assert "C/C++ checker skipped" in check_file(cpp, project_root=tmp_path)


def test_checker_rejects_sensitive_escape_oversize_and_binary(tmp_path: Path):
    secret = tmp_path / ".env"
    secret.write_text("TOKEN=hidden\n", encoding="utf-8")
    assert "issue(s)" in check_file(secret, project_root=tmp_path)
    assert "scope" in check_file(str(tmp_path.parent / "outside.py"), project_root=tmp_path)
    huge = tmp_path / "huge.py"
    huge.write_bytes(b"x" * (1024 * 1024 + 1))
    assert "too_large" in check_file(huge, project_root=tmp_path)
    binary = tmp_path / "binary.py"
    binary.write_bytes(b"x\0y")
    assert "binary" in check_file(binary, project_root=tmp_path)


def test_checker_external_timeout_is_bounded(tmp_path: Path, monkeypatch):
    path = tmp_path / "broken.js"
    path.write_text("const x = ;\n", encoding="utf-8")
    monkeypatch.setattr(code_check.shutil, "which", lambda name: "node")

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs.get("timeout", 0))

    monkeypatch.setattr(code_check.subprocess, "run", timeout)
    started = time.monotonic()
    output = check_file(path, project_root=tmp_path)
    assert time.monotonic() - started < 10
    assert "timeout" in output


def test_node_and_tsc_output_is_location_parsed(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(code_check.shutil, "which", lambda name: name)

    def fake_run(command, **kwargs):
        result = subprocess.CompletedProcess(command, 1, "", "bad.js:4:9: syntax error")
        if command[0] == "tsc":
            result = subprocess.CompletedProcess(command, 1, "x.ts(7,3): error TS1005: expected", "")
        return result

    monkeypatch.setattr(code_check.subprocess, "run", fake_run)
    js = tmp_path / "bad.js"
    js.write_text("const x = 1;\n", encoding="utf-8")
    ts = tmp_path / "bad.ts"
    ts.write_text("const x: = 1;\n", encoding="utf-8")
    assert "4:9: node" in check_file(js, project_root=tmp_path)
    assert "7:3: tsc TS1005" in check_file(ts, project_root=tmp_path)


def test_max_items_sorts_and_bounds_report(monkeypatch, tmp_path: Path):
    path = tmp_path / "data.json"
    path.write_text("{}", encoding="utf-8")
    issues = [code_check.CheckIssue(line, 1, "json", "E", "x" * 100) for line in (9, 2, 5)]
    monkeypatch.setattr(code_check, "_parse_json", lambda *_: issues)
    output = check_file(path, 2, project_root=tmp_path)
    assert "2:1:" in output and "5:1:" in output and "9:1:" not in output
    assert len(output) <= 2000


@pytest.mark.parametrize(("pyflakes", "expected"), [(True, "pyflakes"), (False, "py_compile")],
                         ids=["pyflakes-installed", "nothing-installed"])
def test_python_clean_linter_fallback_never_claims_ruff(monkeypatch, tmp_path: Path, pyflakes, expected):
    import harness_tools
    # Ruff is unavailable to both probes: off PATH and not importable.
    _pin_detect_linter_probes(monkeypatch, pyflakes=pyflakes)
    assert harness_tools._detect_linter(tmp_path) == expected


@pytest.mark.parametrize(("on_path", "as_module"), [(True, False), (False, True)],
                         ids=["ruff-on-path", "ruff-module-only"])
def test_python_linter_detection_names_ruff_when_available(monkeypatch, tmp_path: Path, on_path, as_module):
    import harness_tools
    # pyflakes is installed too: an available ruff still takes precedence.
    _pin_detect_linter_probes(monkeypatch, ruff_on_path=on_path, ruff_module=as_module, pyflakes=True)
    assert harness_tools._detect_linter(tmp_path) == "ruff"


def test_project_root_refuses_escape(tmp_path: Path):
    assert "scope" in check_file(str(tmp_path.parent / "outside.py"), project_root=tmp_path)


def test_post_edit_hook_requires_gate_and_only_checks_successful_target():
    calls = []

    def dispatch(name, args):
        calls.append((name, args))
        return "file_check x.py: none"

    assert post_edit_file_check("file_write", {"path": "x.py"}, "edited", dispatch, gate=lambda *_: False).endswith("permission required)")
    assert calls == []
    assert post_edit_file_check("file_write", {"path": "x.txt"}, "edited", dispatch) == "edited"
    assert post_edit_file_check("file_write", {"path": "x.py"}, "edited", dispatch) == "edited\nfile_check: none"
    assert calls == [("file_check", {"path": "x.py", "max_items": 30})]


def test_post_edit_hook_runs_real_checker_for_bad_and_clean_edits(monkeypatch, tmp_path: Path):
    # The real checker, without whichever external linter this machine has.
    _pin_file_check_linters(monkeypatch)
    path = tmp_path / "edited.py"
    path.write_text("value = 1\n\ndef f(:\n    pass\n", encoding="utf-8")
    calls = []

    def dispatch(name, args):
        calls.append((name, args))
        return check_file(args["path"], args["max_items"], project_root=tmp_path)

    observation = post_edit_file_check("file_edit", {"path": "edited.py"}, "edited", dispatch)
    assert "edited" in observation
    assert "file_check: 1 issue(s)" in observation
    assert "3:7: python SyntaxError" in observation

    path.write_text("def f():\n    return 1\n", encoding="utf-8")
    clean = post_edit_file_check("file_edit", {"path": "edited.py"}, "edited", dispatch)
    assert clean.endswith("file_check: none")
    assert len(calls) == 2


def test_post_edit_hook_skips_failed_preview_and_preserves_long_observation(tmp_path: Path):
    calls = []
    dispatch = lambda name, args: calls.append((name, args)) or "file_check x.py: none"
    long_observation = "x" * 1000
    assert post_edit_file_check("file_edit", {"path": "x.py"}, "ERROR: failed", dispatch) == "ERROR: failed"
    assert post_edit_file_check("file_edit", {"path": "x.py", "dry_run": True}, "preview", dispatch) == "preview"
    result = post_edit_file_check("file_edit", {"path": "x.py"}, long_observation, dispatch)
    assert result.startswith(long_observation)
    assert "file_check: none" in result
    assert calls == [("file_check", {"path": "x.py", "max_items": 30})]


@pytest.mark.parametrize("tool, args", [
    ("text_patch", {"root": ".", "patch": "--- a/changed.py\n+++ b/changed.py\n@@ -1 +1 @@\n-old\n+new\n", "apply": True}),
    ("apply_patch", {"patch_text": "*** Update File: changed.py\n"}),
])
def test_patch_targets_are_checked_only_when_applied(tool, args):
    calls = []
    dispatch = lambda name, payload: calls.append((name, payload)) or "file_check changed.py: none"
    result = post_edit_file_check(tool, args, "applied", dispatch)
    assert "file_check" in result


def test_observed_edit_detects_syntax_in_the_same_observation(monkeypatch, tmp_path, unattended_effects_allowed):
    """Exercise the real host hook and file-check dispatcher, with only editing faked."""
    import permission_modes
    import server
    # Qualify the required membership independently of the separate surface
    # assertion, which stays RED until that ownership-scoped edit is integrated.
    monkeypatch.setattr(permission_modes, "EXECUTION_TOOLS", permission_modes.EXECUTION_TOOLS | {"file_check"})
    # The real checker, without whichever external linter this machine has.
    _pin_file_check_linters(monkeypatch)
    path = tmp_path / "edited.py"

    def edit(path, old, new, **kwargs):
        Path(path).write_text(new, encoding="utf-8")
        return "Edited file"

    monkeypatch.setattr(server, "file_edit", edit)
    broken = server._agent_dispatch_observed(
        "file_edit", {"path": "edited.py", "old": "old", "new": "x = 1\n\ndef f(:\n    pass\n"},
        project=str(tmp_path))
    assert "\nfile_check: 1 issue(s)\n3:7: python SyntaxError" in broken
    clean = server._agent_dispatch_observed(
        "file_edit", {"path": "edited.py", "old": "old", "new": "def f():\n    return 1\n"},
        project=str(tmp_path))
    assert clean.endswith("file_check: none")
    assert path.read_text(encoding="utf-8").startswith("def f():")


def test_observed_file_check_obeys_execution_gate_and_scope(monkeypatch, tmp_path):
    import permission_modes
    import server
    monkeypatch.setattr(permission_modes, "EXECUTION_TOOLS", permission_modes.EXECUTION_TOOLS | {"file_check"})
    monkeypatch.setattr(permission_modes, "current_mode", lambda: "plan")
    calls = []
    monkeypatch.setattr(code_check, "_syntax_python", lambda *args: calls.append(args) or [])
    denied = server._agent_dispatch_observed("file_check", {"path": "x.py"}, project=str(tmp_path))
    assert denied.startswith("ERROR:")
    assert not calls
    monkeypatch.setattr(permission_modes, "current_mode", lambda: "auto")
    escaped = server._agent_dispatch_observed(
        "file_check", {"path": str(tmp_path.parent / "outside.py")}, project=str(tmp_path))
    assert escaped.startswith("ERROR:")
    assert not calls


def test_read_only_worker_refuses_file_check_before_checker_dispatch(monkeypatch, tmp_path):
    """Read-only fleet workers must never reach subprocess-capable checkers."""
    import server
    import permission_modes
    import sonder_runtime.bootstrap.code_check_agent_tools as check_tools

    calls = []
    monkeypatch.setattr(permission_modes, "current_mode", lambda: "auto")
    monkeypatch.setattr(check_tools, "dispatch_file_check", lambda *a, **k: calls.append((a, k)))
    direct = server._agent_dispatch(
        "file_check", {"path": "x.py"}, read_only=True,
        repository_extra_roots=str(tmp_path),
    )
    observed = server._agent_dispatch_observed(
        "file_check", {"path": "x.py"}, read_only=True, project=str(tmp_path),
    )
    assert direct.startswith("ERROR:")
    assert "repository read-only" in direct
    assert observed.startswith("ERROR:")
    assert calls == []


def test_missing_execution_registration_fails_closed(monkeypatch, tmp_path):
    import permission_modes
    from sonder_runtime.bootstrap.code_check_agent_tools import dispatch_file_check
    monkeypatch.setattr(permission_modes, "EXECUTION_TOOLS", permission_modes.EXECUTION_TOOLS - {"file_check"})
    assert dispatch_file_check("x.py", project_root=tmp_path).startswith("ERROR:")


def test_ruff_cannot_apply_configured_fixes(monkeypatch, tmp_path):
    path = tmp_path / "file.py"
    path.write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setattr(code_check.importlib.util, "find_spec", lambda name: None)
    monkeypatch.setattr(code_check, "_ruff_path", lambda: "ruff")
    commands = []
    monkeypatch.setattr(code_check, "_external", lambda command, *args: commands.append(command) or [])
    assert check_file(path, project_root=tmp_path).endswith(": none")
    assert {"--no-fix", "--no-cache", "--isolated"} <= set(commands[0])


def test_native_help_and_file_check_have_working_handlers(monkeypatch, tmp_path):
    import permission_modes
    from sonder_runtime.bootstrap.code_check_agent_tools import native_agent_assist
    from sonder_runtime.bootstrap.native_mcp import native_tool_registry
    registry = native_tool_registry()
    help_result = native_agent_assist("tool_help", {"name": "file_check"}, registry, (tmp_path,))
    assert not help_result["isError"]
    assert "path (required)" in help_result["output"]
    assert native_agent_assist("file_check", {"path": "x.py"}, registry, ())["isError"]
    monkeypatch.setattr(permission_modes, "EXECUTION_TOOLS", permission_modes.EXECUTION_TOOLS | {"file_check"})
    (tmp_path / "x.json").write_text("{}", encoding="utf-8")
    result = native_agent_assist("file_check", {"path": "x.json"}, registry, (tmp_path,))
    assert result["output"] == "file_check x.json: none"


def test_patch_sixth_file_error_is_not_hidden_by_five_clean_files():
    calls = []
    patch = "".join(f"--- a/{i}.py\n+++ b/{i}.py\n@@ -1 +1 @@\n-old\n+new\n" for i in range(6))

    def dispatch(name, args):
        calls.append(args["path"])
        if args["path"].endswith("5.py"):
            return "file_check 5.py: 1 issue(s)\n3:7: python SyntaxError invalid syntax"
        return "file_check clean.py: none"

    output = post_edit_file_check("text_patch", {"patch": patch, "apply": True}, "applied", dispatch)
    assert len(calls) == 6
    assert "file_check: 1 issue(s)" in output
    assert "SyntaxError" in output
