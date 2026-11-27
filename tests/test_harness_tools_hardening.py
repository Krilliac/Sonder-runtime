"""Developer-tool hardening in ``harness_tools`` and the secret-scan adapter.

Covers option-shaped arguments, read-only format checks, isolated project pip
execution, git ref/-- handling, isolated python -m invocations, pip-audit without
pip/pyproject installs, read guards on diff_files / extract_references /
rename_symbol, Windows PATHEXT + BatBadBut defenses, bounded process-tree
timeouts, and nested .env handling in secret_scan.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import textwrap
import time

import harness_tools
import pytest

from sonder_runtime.adapters import confined_read
from sonder_runtime.adapters import secret_scan as secret_scan_adapter
from sonder_runtime.adapters.filesystem import file_ops

MARK = "PLANTEDKEYMARK"


@pytest.fixture(autouse=True)
def _authorize_pytest_tmp_roots(tmp_path_factory, monkeypatch):
    # Authorizes pytest's tmp tree, as tests/test_harness_dev.py does; the
    # confinement itself is asserted by tests/test_harness_root_confinement.py.
    monkeypatch.setenv("SONDER_FILE_ROOTS", str(tmp_path_factory.getbasetemp()))
    monkeypatch.delenv("SONDER_FILE_BYPASS", raising=False)


@pytest.fixture
def captured(monkeypatch):
    calls = []

    def run(cmd, *, cwd, timeout=None, **kwargs):
        calls.append([str(c) for c in cmd])
        return {"ok": True, "returncode": 0, "stdout": "", "stderr": "", "command": list(cmd)}

    monkeypatch.setattr(harness_tools, "_run", run)
    return calls


@pytest.fixture
def no_child(monkeypatch):
    monkeypatch.setattr(harness_tools, "_run", lambda *a, **k: pytest.fail(f"argv reached _run: {a!r}"))
    monkeypatch.setattr(harness_tools, "_run_git", lambda *a, **k: pytest.fail(f"argv reached git: {a!r}"))


@pytest.fixture
def not_live(monkeypatch):
    monkeypatch.setattr(harness_tools, "_live_refusal", lambda operation, root: None)


# ---- option-shaped path ----------------------------------------------------

_PATH_TOOLS = ("test_run", "lint_run", "format_code", "typecheck_run")


@pytest.mark.parametrize("tool", _PATH_TOOLS)
@pytest.mark.parametrize("path", ["--basetemp=elsewhere", "-p", "-cconfig.ini", "sub/../-pevil"])
def test_option_shaped_path_never_reaches_argv(tmp_path, no_child, tool, path):
    (tmp_path / "test_x.py").write_text("def test_x(): pass\n", encoding="utf-8")
    with pytest.raises(ValueError, match="start with '-'"):
        getattr(harness_tools, tool)(root=str(tmp_path), path=path)


def test_lint_run_path_cannot_switch_on_fix_mode(tmp_path, no_child):
    with pytest.raises(ValueError):
        harness_tools.lint_run(root=str(tmp_path), tool="ruff", path="--fix")


def test_ordinary_path_still_reaches_argv(tmp_path, captured):
    (tmp_path / "pkg").mkdir()
    harness_tools.lint_run(root=str(tmp_path), tool="flake8", path="pkg")
    assert captured[-1][-1] == "pkg"


# ---- format_code check_only ------------------------------------------------

@pytest.mark.parametrize("tool, check, write", [
    ("gofmt", ["gofmt", "-l"], "-w"),
    ("clang-format", ["clang-format", "--dry-run", "--Werror"], "-i"),
])
def test_format_check_only_never_runs_a_writing_formatter(tmp_path, captured, tool, check, write):
    (tmp_path / "a.c").write_text("int main(){}\n", encoding="utf-8")
    result = harness_tools.format_code(root=str(tmp_path), tool=tool, path="a.c", check_only=True)
    assert result["mode"] == "check"
    assert captured == [check + ["a.c"]]
    assert write not in captured[0]


@pytest.mark.parametrize("tool, check", [
    ("ruff", ["ruff", "format", "--check", "."]),
    ("black", ["black", "--check", "."]),
    ("prettier", ["npx", "--no-install", "prettier", "--check", "."]),
    ("biome", ["npx", "--no-install", "@biomejs/biome", "format", "."]),
    ("dart", ["dart", "format", "--output=none", "--set-exit-if-changed", "."]),
])
def test_format_check_only_keeps_the_existing_check_forms(tmp_path, captured, tool, check):
    result = harness_tools.format_code(root=str(tmp_path), tool=tool, check_only=True)
    assert result["ok"] is True
    assert captured == [check]


@pytest.mark.parametrize("tool", ["dotnet", "rustfmt"])
def test_format_check_only_refuses_project_evaluating_formatters(tmp_path, no_child, tool):
    result = harness_tools.format_code(root=str(tmp_path), tool=tool, check_only=True)
    assert result["ok"] is False
    assert "refusing to run" in result["error"]


@pytest.mark.parametrize("tool", ["zig", "swift-format", "mix", "shfmt", "stylua", "ktlint", "rubocop"])
def test_format_check_only_refuses_formatters_without_check_form(tmp_path, no_child, tool):
    result = harness_tools.format_code(root=str(tmp_path), tool=tool, check_only=True)
    assert result["ok"] is False
    assert result["mode"] == "check"
    assert "check-only" in result["error"]


def test_gofmt_check_fails_when_files_need_formatting(tmp_path, monkeypatch):
    calls = []

    def run(cmd, **k):
        calls.append(list(cmd))
        return {"ok": True, "returncode": 0, "stdout": "main.go\n", "stderr": "", "command": cmd}

    monkeypatch.setattr(harness_tools, "_run", run)
    result = harness_tools.format_code(root=str(tmp_path), tool="gofmt", check_only=True)
    assert result["ok"] is False
    assert calls == [["gofmt", "-l", "."]]


def test_gofmt_check_without_path_walks_the_root_not_stdin(tmp_path, captured):
    # ``gofmt -l`` with no argument reads the empty stdin and passes vacuously.
    harness_tools.format_code(root=str(tmp_path), tool="gofmt", check_only=True)
    assert captured == [["gofmt", "-l", "."]]


def test_clang_format_check_without_path_is_refused(tmp_path, no_child):
    # ``clang-format --dry-run --Werror`` with no file reads the empty stdin
    # and exits 0, and it has no directory mode to fall back on.
    result = harness_tools.format_code(root=str(tmp_path), tool="clang-format", check_only=True)
    assert result["ok"] is False
    assert "path required" in result["error"]


# ---- pip dependency tools --------------------------------------------------

def _fake_venv(root, name=".venv", *, pth=None):
    """A venv-shaped folder: pyvenv.cfg, a python binary the runtime must never run, site-packages."""
    venv = root / name
    python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    python.parent.mkdir(parents=True)
    python.write_bytes(b"EVIL-BINARY")
    site = venv / ("Lib/site-packages" if os.name == "nt" else "lib/python3.12/site-packages")
    site.mkdir(parents=True)
    if pth:
        (site / "evil.pth").write_text(pth, encoding="utf-8")
    (venv / "pyvenv.cfg").write_text("home = elsewhere\n", encoding="utf-8")
    return site


_PIP_CALLS = (
    ("dependency_add", {"packages_json": '["requests"]'}),
    ("dependency_remove", {"packages_json": '["requests"]'}),
    ("dependency_update", {"packages_json": "[]"}),
)
_PIP_INSTALL_CALLS = tuple(call for call in _PIP_CALLS if call[0] != "dependency_remove")


@pytest.mark.parametrize("tool, kwargs", _PIP_CALLS)
def test_pip_never_targets_the_runtime_interpreter(tmp_path, no_child, not_live, tool, kwargs):
    (tmp_path / "requirements.txt").write_text("requests\n", encoding="utf-8")
    result = getattr(harness_tools, tool)(root=str(tmp_path), **kwargs)
    assert result["ok"] is False
    assert "virtual environment" in result["error"] or "uninstall" in result["error"]


@pytest.mark.parametrize("tool, kwargs", _PIP_INSTALL_CALLS)
def test_pip_runs_the_runtime_pip_into_the_venv_and_never_the_repo_interpreter(
        tmp_path, captured, not_live, tool, kwargs):
    (tmp_path / "requirements.txt").write_text("requests\n", encoding="utf-8")
    site = _fake_venv(tmp_path, pth="import os; os.system('echo PTH-EXECUTED')\n")
    getattr(harness_tools, tool)(root=str(tmp_path), **kwargs)
    assert len(captured) == 1
    assert captured[0][:6] == [sys.executable, "-I", "-m", "pip", "install", "--target"]
    assert captured[0][6] == str(site)
    assert not any(part.endswith(("python.exe", "bin/python")) and part != sys.executable for part in captured[0])


def test_dependency_remove_is_refused_rather_than_run_in_the_repo_interpreter(tmp_path, no_child, not_live):
    (tmp_path / "requirements.txt").write_text("requests\n", encoding="utf-8")
    _fake_venv(tmp_path)
    result = harness_tools.dependency_remove(root=str(tmp_path), packages_json='["requests"]')
    assert result["ok"] is False
    assert "uninstall" in result["error"]


def test_pip_refuses_a_venv_without_pyvenv_cfg_or_site_packages(tmp_path, no_child, not_live):
    (tmp_path / "requirements.txt").write_text("requests\n", encoding="utf-8")
    site = _fake_venv(tmp_path)
    (tmp_path / ".venv" / "pyvenv.cfg").unlink()
    assert harness_tools.dependency_add(root=str(tmp_path), packages_json='["requests"]')["ok"] is False
    (tmp_path / ".venv" / "pyvenv.cfg").write_text("home = x\n", encoding="utf-8")
    shutil.rmtree(site)
    assert harness_tools.dependency_add(root=str(tmp_path), packages_json='["requests"]')["ok"] is False


@pytest.mark.skipif(os.name != "nt", reason="junction creation is Windows-specific here")
def test_pip_refuses_a_site_packages_junction_to_outside_the_root(tmp_path, no_child, not_live):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "requirements.txt").write_text("requests\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    venv = root / ".venv"
    (venv / "Lib").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("home = x\n", encoding="utf-8")
    subprocess.run(["cmd", "/c", "mklink", "/J", str(venv / "Lib" / "site-packages"), str(outside)],
                   check=True, capture_output=True)
    result = harness_tools.dependency_add(root=str(root), packages_json='["requests"]')
    assert result["ok"] is False
    assert "virtual environment" in result["error"]


def test_pip_refuses_a_venv_that_is_the_runtime_environment(tmp_path, no_child, not_live, monkeypatch):
    (tmp_path / "requirements.txt").write_text("requests\n", encoding="utf-8")
    _fake_venv(tmp_path, "venv")
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "venv"))
    result = harness_tools.dependency_add(root=str(tmp_path), packages_json='["requests"]')
    assert result["ok"] is False


def test_dependency_mutations_still_enforce_protected_write_scope(tmp_path, no_child, not_live, monkeypatch):
    (tmp_path / "requirements.txt").write_text("requests\n", encoding="utf-8")
    _fake_venv(tmp_path)
    monkeypatch.setattr(
        harness_tools.protected_write_scope,
        "scope_refusal",
        lambda op, _root, _target: f"refusing {op}: protected policy file inside root",
    )
    for tool, kwargs in _PIP_CALLS:
        result = getattr(harness_tools, tool)(root=str(tmp_path), **kwargs)
        assert result["ok"] is False
        assert "protected" in result["error"]


@pytest.mark.parametrize("marker", ["requirements.txt", "package.json"])
@pytest.mark.parametrize("packages", [
    '["--index-url=http://example.invalid/"]', '["-e", "git+https://example.invalid/x"]', "[1]", '[""]',
])
def test_option_shaped_package_names_are_refused(tmp_path, no_child, not_live, marker, packages):
    (tmp_path / marker).write_text("{}\n", encoding="utf-8")
    _fake_venv(tmp_path)
    for tool in ("dependency_add", "dependency_remove", "dependency_update"):
        result = getattr(harness_tools, tool)(root=str(tmp_path), packages_json=packages)
        assert result["ok"] is False, (tool, result)


@pytest.mark.parametrize("tool, kwargs", _PIP_CALLS)
def test_dependency_mutations_honor_the_live_checkout_refusal(tmp_path, no_child, monkeypatch, tool, kwargs):
    seen = []

    def refuse(operation, root):
        seen.append(operation)
        return {"ok": False, "error": "live checkout"}

    monkeypatch.setattr(harness_tools, "_live_refusal", refuse)
    (tmp_path / "requirements.txt").write_text("requests\n", encoding="utf-8")
    _fake_venv(tmp_path)
    result = getattr(harness_tools, tool)(root=str(tmp_path), **kwargs)
    assert result == {"ok": False, "error": "live checkout"}
    assert seen == [tool]


# ---- git refs --------------------------------------------------------------

@pytest.mark.parametrize("tool, kwargs", [
    ("git_checkout", {"ref": "-f"}),
    ("git_checkout", {"ref": "--force"}),
    ("git_branch", {"name": "-D", "base": "main", "checkout": False}),
    ("git_branch", {"name": "topic", "base": "--orphan"}),
    ("git_merge", {"branch": "--abort"}),
    ("git_cherry_pick", {"commits_json": '["--abort"]'}),
    ("git_cherry_pick", {"commits_json": "[1]"}),
    ("git_tag", {"name": "-d"}),
])
def test_git_values_cannot_be_options(tmp_path, no_child, not_live, tool, kwargs):
    (tmp_path / ".git").mkdir()
    result = getattr(harness_tools, tool)(root=str(tmp_path), **kwargs)
    assert result["ok"] is False


def test_git_checkout_reads_ref_as_a_revision_not_a_pathspec(tmp_path, not_live, monkeypatch):
    (tmp_path / ".git").mkdir()
    calls = []
    monkeypatch.setattr(harness_tools, "_run_git", lambda root, args, **k: calls.append(list(args)) or {"ok": True})
    harness_tools.git_checkout(root=str(tmp_path), ref=".")
    assert calls == [["checkout", ".", "--"]]


# ---- -I on python -m -------------------------------------------------------

def test_typecheck_does_not_run_a_root_local_mypy_module(tmp_path):
    marker = tmp_path / "SHADOW_RAN"
    (tmp_path / "mypy.py").write_text(
        f"open({str(marker)!r}, 'w').write('ran')\n", encoding="utf-8",
    )
    harness_tools.typecheck_run(root=str(tmp_path), tool="mypy", timeout=60)
    assert not marker.exists()


def test_lint_pyflakes_does_not_run_a_root_local_pyflakes_module(tmp_path):
    marker = tmp_path / "SHADOW_RAN"
    (tmp_path / "pyflakes.py").write_text(
        f"open({str(marker)!r}, 'w').write('ran')\n", encoding="utf-8",
    )
    harness_tools.lint_run(root=str(tmp_path), tool="pyflakes", timeout=60)
    assert not marker.exists()


# ---- pip-audit -------------------------------------------------------------

def test_dependency_audit_runs_pip_audit_without_pip(tmp_path, captured, monkeypatch):
    # Without --no-deps --disable-pip, pip-audit pip-installs the requirements
    # into a temporary environment (sdist build code runs) from a read-only tool.
    fake = str(tmp_path / "bin" / "pip-audit")
    monkeypatch.setattr(harness_tools.shutil, "which", lambda name, *a, **k: fake if name == "pip-audit" else None)
    (tmp_path / "requirements.txt").write_text("\n", encoding="utf-8")
    result = harness_tools.dependency_audit(root=str(tmp_path))
    assert captured == [[fake, "-r", "requirements.txt", "--no-deps", "--disable-pip"]]
    assert result["manager"] == "pip"


def test_dependency_audit_refuses_a_pyproject_only_project(tmp_path, no_child, monkeypatch):
    # ``pip-audit <project dir>`` always pip-installs the project's dependencies.
    fake = str(tmp_path / "bin" / "pip-audit")
    monkeypatch.setattr(harness_tools.shutil, "which", lambda name, *a, **k: fake if name == "pip-audit" else None)
    (tmp_path / "pyproject.toml").write_text("\n", encoding="utf-8")
    result = harness_tools.dependency_audit(root=str(tmp_path))
    assert result["ok"] is False
    assert "requirements.txt" in result["error"]


def test_dependency_audit_reports_a_missing_pip_audit(tmp_path, no_child, monkeypatch):
    monkeypatch.setattr(harness_tools.shutil, "which", lambda name, *a, **k: None)
    (tmp_path / "requirements.txt").write_text("\n", encoding="utf-8")
    result = harness_tools.dependency_audit(root=str(tmp_path))
    assert result["ok"] is False
    assert "pip-audit not installed" in result["error"]


def test_dependency_audit_refuses_symlink_escaping_requirements(tmp_path, no_child, monkeypatch):
    proj = tmp_path / "proj"
    proj.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("requests==2.32.0\n", encoding="utf-8")
    try:
        (proj / "requirements.txt").symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks need privilege on this host")
    fake = str(tmp_path / "bin" / "pip-audit")
    monkeypatch.setattr(harness_tools.shutil, "which", lambda name, *a, **k: fake if name == "pip-audit" else None)
    result = harness_tools.dependency_audit(root=str(proj))
    assert result["ok"] is False
    assert "link" in result["error"] or "outside" in result["error"]


# ---- diff_files / extract_references read guard ---------------------------

@pytest.fixture
def workspace(tmp_path, monkeypatch):
    ws = tmp_path / "workspace"
    home = tmp_path / "home"
    ws.mkdir()
    home.mkdir()
    monkeypatch.setattr(file_ops, "workspace_root", lambda: ws)
    monkeypatch.setattr(file_ops.runtime_paths, "default_home", lambda: home)
    monkeypatch.setenv("SONDER_FILE_ROOTS_FILE", str(home / "file_roots.local"))
    (ws / "credentials.json").write_text(f'{{"key": "{MARK}"}}\n', encoding="utf-8")
    (ws / "id_ed25519").write_text(f"key {MARK}\n", encoding="utf-8")
    (ws / "src").mkdir()
    (ws / "src" / "ok.txt").write_text("key ordinary\n", encoding="utf-8")
    return ws


def test_extract_references_skips_credential_and_protected_files(workspace):
    result = harness_tools.extract_references(root=str(workspace), symbol="key", glob="**/*")
    files = {ref["file"] for ref in result["references"]}
    assert files == {os.path.join("src", "ok.txt")}
    assert MARK not in str(result)


@pytest.mark.parametrize("dry_run", [True, False])
def test_rename_symbol_refuses_a_matching_credential_file(workspace, dry_run):
    before = (workspace / "credentials.json").read_text(encoding="utf-8")
    result = harness_tools.rename_symbol(
        root=str(workspace), old_name="key", new_name="k2", glob="**/*", dry_run=dry_run,
    )
    assert result["ok"] is False
    assert MARK not in str(result)
    assert (workspace / "credentials.json").read_text(encoding="utf-8") == before
    assert (workspace / "src" / "ok.txt").read_text(encoding="utf-8") == "key ordinary\n"


@pytest.mark.parametrize("dry_run", [True, False])
def test_rename_symbol_refusal_does_not_say_what_a_credential_file_holds(workspace, dry_run):
    # Refusing only when the protected file matched made the answer an oracle
    # for the words it contains; the refusal must not depend on its contents.
    present = harness_tools.rename_symbol(
        root=str(workspace), old_name=MARK, new_name="k2", glob="**/*", dry_run=dry_run,
    )
    absent = harness_tools.rename_symbol(
        root=str(workspace), old_name="wordnotinanyfile", new_name="k2", glob="**/*", dry_run=dry_run,
    )
    assert present["ok"] is False and absent["ok"] is False
    assert present["error"] == absent["error"]
    assert "credentials.json" not in present["error"]
    assert "preview" not in present


def test_rename_symbol_still_renames_ordinary_files(workspace):
    result = harness_tools.rename_symbol(
        root=str(workspace), old_name="key", new_name="k2", glob="src/**/*", dry_run=False,
    )
    assert result["ok"] is True, result
    assert (workspace / "src" / "ok.txt").read_text(encoding="utf-8") == "k2 ordinary\n"


def test_rename_symbol_refuses_a_link_without_reading_it(workspace, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text(f"key {MARK}\n", encoding="utf-8")
    try:
        (workspace / "src" / "link.txt").symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks need privilege on this host")
    result = harness_tools.rename_symbol(
        root=str(workspace), old_name="key", new_name="k2", glob="src/**/*", dry_run=True,
    )
    assert result["ok"] is False
    assert MARK not in str(result)
    assert (workspace / "src" / "ok.txt").read_text(encoding="utf-8") == "key ordinary\n"


@pytest.mark.parametrize("secret", ["credentials.json", "id_ed25519"])
@pytest.mark.parametrize("side", ["left", "right"])
def test_diff_files_refuses_credential_and_protected_files(workspace, secret, side):
    other = os.path.join("src", "ok.txt")
    kwargs = {"left": secret, "right": other} if side == "left" else {"left": other, "right": secret}
    result = harness_tools.diff_files(root=str(workspace), **kwargs)
    assert result["ok"] is False
    assert MARK not in str(result)


@pytest.mark.parametrize("side", ["left", "right"])
def test_diff_files_refusal_does_not_say_whether_a_credential_file_exists(workspace, side):
    other = os.path.join("src", "ok.txt")
    kwargs = {"left": "id_rsa", "right": other} if side == "left" else {"left": other, "right": "id_rsa"}
    missing = harness_tools.diff_files(root=str(workspace), **kwargs)
    (workspace / "id_rsa").write_text(f"key {MARK}\n", encoding="utf-8")
    present = harness_tools.diff_files(root=str(workspace), **kwargs)
    assert missing["ok"] is False and present["ok"] is False
    assert missing["error"] == present["error"]


@pytest.mark.parametrize("bad", [r"\\attacker.invalid\share\secret.txt", "//attacker.invalid/share/x", "a\x00b.txt"])
def test_diff_files_refuses_network_or_control_paths_before_resolving(workspace, no_child, bad):
    other = os.path.join("src", "ok.txt")
    result = harness_tools.diff_files(root=str(workspace), left=bad, right=other)
    assert result["ok"] is False


def test_diff_files_bounds_negative_context(workspace):
    other = os.path.join("src", "ok.txt")
    (workspace / "src" / "other.txt").write_text("key two\n", encoding="utf-8")
    result = harness_tools.diff_files(root=str(workspace), left=other, right="src/other.txt", context=-5)
    assert result["ok"] is True
    assert "-key ordinary" in result["stdout"] and "+key two" in result["stdout"]


# ---- PATHEXT resolution on Windows -----------------------------------------

@pytest.mark.skipif(os.name != "nt", reason="PATHEXT and .cmd shims are Windows behavior")
def test_cmd_shims_are_found_through_pathext(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "fakenodetool.cmd").write_text("@echo shim-ran %1\r\n", encoding="utf-8")
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ.get("PATH", ""))
    result = harness_tools._run(["fakenodetool", "value"], cwd=tmp_path, timeout=20)
    assert result["ok"] is True, result
    assert "shim-ran value" in result["stdout"]


@pytest.mark.skipif(os.name != "nt", reason="PATHEXT resolution with directory prefix")
def test_directory_prefixed_cmd_shim_without_extension_is_resolved_and_guarded(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "fakenodetool.cmd").write_text("@echo shim-ran %*\r\n", encoding="utf-8")
    ok = harness_tools._run([str(bindir / "fakenodetool"), "value"], cwd=tmp_path, timeout=20)
    assert ok["ok"] is True, ok
    assert "shim-ran value" in ok["stdout"]
    marker = tmp_path / "INJECTED"
    bad = harness_tools._run([str(bindir / "fakenodetool"), f"x&echo>{marker}"], cwd=tmp_path, timeout=20)
    assert bad["ok"] is False
    assert "cmd.exe would interpret" in bad["stderr"]
    assert not marker.exists()


@pytest.mark.skipif(os.name != "nt", reason="cmd.exe batch argument parsing is Windows behavior")
def test_cmd_shim_refuses_arguments_cmd_would_interpret(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "fakenodetool.cmd").write_text("@echo shim-ran %*\r\n", encoding="utf-8")
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ.get("PATH", ""))
    marker = tmp_path / "INJECTED"
    result = harness_tools._run(["fakenodetool", f"x&echo>{marker}"], cwd=tmp_path, timeout=20)
    assert result["ok"] is False
    assert "cmd.exe would interpret" in result["stderr"]
    assert not marker.exists()


@pytest.mark.skipif(os.name != "nt", reason="cmd.exe batch argument parsing is Windows behavior")
def test_batch_file_named_by_path_refuses_arguments_cmd_would_interpret(tmp_path):
    shim = tmp_path / "fakenodetool.cmd"
    shim.write_text("@echo shim-ran %*\r\n", encoding="utf-8")
    marker = tmp_path / "INJECTED"
    result = harness_tools._run([str(shim), f"x&echo>{marker}"], cwd=tmp_path, timeout=20)
    assert result["ok"] is False
    assert "cmd.exe would interpret" in result["stderr"]
    assert not marker.exists()


# ---- timeout bounds the call and kills the tree ----------------------------

def test_timeout_kills_a_grandchild_holding_the_pipes(tmp_path):
    marker = tmp_path / "GRANDCHILD_SURVIVED"
    grandchild = f"import time; time.sleep(8); open({str(marker)!r}, 'w').write('x')"
    child = textwrap.dedent(f"""
        import subprocess, sys, time
        subprocess.Popen([sys.executable, "-c", {grandchild!r}], stdout=sys.stdout, stderr=sys.stderr, close_fds=False)
        time.sleep(30)
    """)
    started = time.monotonic()
    result = harness_tools._run([sys.executable, "-c", child], cwd=tmp_path, timeout=2)
    elapsed = time.monotonic() - started
    assert result["timed_out"] is True
    assert elapsed < 7, elapsed
    # The grandchild would write its marker 8 s after it started.
    time.sleep(max(0.0, 10 - (time.monotonic() - started)))
    assert not marker.exists()


# ---- nested .env files in the secret scan ----------------------------------

def _env_tree(root):
    (root / "config").mkdir()
    (root / "config" / ".env").write_text(f"API_KEY={MARK}-value\n", encoding="utf-8")
    (root / "services" / "api").mkdir(parents=True)
    (root / "services" / "api" / ".env.local").write_text(f"API_KEY={MARK}-value\n", encoding="utf-8")
    (root / ".hidden").mkdir()
    (root / ".hidden" / ".env").write_text(f"API_KEY={MARK}-value\n", encoding="utf-8")
    (root / ".git").mkdir()
    (root / ".git" / "config.env").write_text(f"API_KEY={MARK}-value\n", encoding="utf-8")


def test_adapter_scan_counts_nested_env_files(tmp_path):
    _env_tree(tmp_path)
    result = secret_scan_adapter.scan(str(tmp_path))
    assert result["credential_files_skipped"] == 3
    assert MARK not in str(result)


def test_harness_secret_scan_counts_nested_env_files(tmp_path):
    _env_tree(tmp_path)
    result = harness_tools.secret_scan(root=str(tmp_path))
    assert result["credential_files_skipped"] == 3
    assert MARK not in str(result)


# ---- requirements includes -------------------------------------------------

def _pip_audit(monkeypatch, tmp_path):
    fake = str(tmp_path / "bin" / "pip-audit")
    monkeypatch.setattr(harness_tools.shutil, "which", lambda name, *a, **k: fake if name == "pip-audit" else None)
    return fake


@pytest.mark.parametrize("name, text, needle", [
    ("nested-escape", "-r ../outside.txt\n", "outside the root"),
    ("nested-absolute", "-r {outside}\n", "outside the root"),
    ("constraint-escape", "-c ../outside.txt\n", "outside the root"),
    ("url-include", "-r https://example.invalid/r.txt\n", "URL"),
    ("editable", "-e .\n", "editable"),
    ("editable-long", "--editable=./pkg\n", "editable"),
    ("index", "--index-url https://example.invalid/simple\nrequests\n", "not allowed"),
    ("extra-index", "--extra-index-url=https://example.invalid/simple\n", "not allowed"),
    ("find-links", "-f https://example.invalid/wheels\n", "not allowed"),
    ("direct-url", "requests @ https://example.invalid/r.whl\n", "link, path or unsupported"),
    ("vcs", "git+https://example.invalid/r.git#egg=r\n", "link, path or unsupported"),
    ("local-path", "./vendored/pkg\n", "link, path or unsupported"),
    ("missing-include", "-r absent.txt\n", "does not exist"),
])
def test_requirements_includes_and_links_are_refused(tmp_path, no_child, monkeypatch, name, text, needle):
    proj = tmp_path / "proj"
    proj.mkdir()
    (tmp_path / "outside.txt").write_text("requests==2.32.0\n", encoding="utf-8")
    (proj / "requirements.txt").write_text(text.format(outside=str(tmp_path / "outside.txt")), encoding="utf-8")
    _pip_audit(monkeypatch, tmp_path)
    result = harness_tools.dependency_audit(root=str(proj))
    assert result["ok"] is False, result
    assert needle in result["error"], result["error"]


def test_a_nested_include_is_checked_recursively(tmp_path, no_child, monkeypatch):
    proj = tmp_path / "proj"
    (proj / "reqs").mkdir(parents=True)
    (proj / "requirements.txt").write_text("-r reqs/base.txt\n", encoding="utf-8")
    (proj / "reqs" / "base.txt").write_text("requests==2.32.0\n-r deeper.txt\n", encoding="utf-8")
    (proj / "reqs" / "deeper.txt").write_text("-e ./evil\n", encoding="utf-8")
    _pip_audit(monkeypatch, tmp_path)
    result = harness_tools.dependency_audit(root=str(proj))
    assert result["ok"] is False
    assert "editable" in result["error"]


def test_a_symlinked_include_is_refused(tmp_path, no_child, monkeypatch):
    proj = tmp_path / "proj"
    proj.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("requests==2.32.0\n", encoding="utf-8")
    try:
        (proj / "linked.txt").symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks need privilege on this host")
    (proj / "requirements.txt").write_text("-r linked.txt\n", encoding="utf-8")
    _pip_audit(monkeypatch, tmp_path)
    assert harness_tools.dependency_audit(root=str(proj))["ok"] is False


def test_ordinary_nested_requirements_with_hashes_and_markers_pass(tmp_path, captured, monkeypatch):
    proj = tmp_path / "proj"
    (proj / "reqs").mkdir(parents=True)
    (proj / "requirements.txt").write_text(
        "# top\n-r reqs/base.txt  # include\n-c reqs/constraints.txt\nrequests[socks]>=2.31,<3 ; python_version >= '3.9'\n",
        encoding="utf-8")
    (proj / "reqs" / "base.txt").write_text(
        "urllib3==2.2.1 \\\n    --hash=sha256:abc123\n--require-hashes\n", encoding="utf-8")
    (proj / "reqs" / "constraints.txt").write_text("idna<4\n", encoding="utf-8")
    fake = _pip_audit(monkeypatch, tmp_path)
    harness_tools.dependency_audit(root=str(proj))
    assert captured == [[fake, "-r", "requirements.txt", "--no-deps", "--disable-pip"]]


def test_an_include_cycle_terminates(tmp_path, captured, monkeypatch):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "requirements.txt").write_text("-r other.txt\n", encoding="utf-8")
    (proj / "other.txt").write_text("-r requirements.txt\nrequests\n", encoding="utf-8")
    _pip_audit(monkeypatch, tmp_path)
    harness_tools.dependency_audit(root=str(proj))
    assert len(captured) == 1


def test_dependency_update_refuses_an_escaping_include_before_running_pip(tmp_path, no_child, not_live):
    (tmp_path / "requirements.txt").write_text("-r ../x.txt\n", encoding="utf-8")
    _fake_venv(tmp_path)
    result = harness_tools.dependency_update(root=str(tmp_path), packages_json="[]")
    assert result["ok"] is False
    assert "outside the root" in result["error"]


# ---- format_code check mode runs under the same guards --------------------

def test_format_check_only_gets_no_guard_exemption(tmp_path, no_child, monkeypatch):
    monkeypatch.setattr(harness_tools, "_live_refusal", lambda op, root: {"ok": False, "error": "live checkout"})
    result = harness_tools.format_code(root=str(tmp_path), tool="ruff", check_only=True)
    assert result["ok"] is False
    assert result["error"] == "live checkout"
    assert result["mode"] == "check"


def test_format_check_only_honors_the_protected_write_scope(tmp_path, no_child, not_live, monkeypatch):
    monkeypatch.setattr(harness_tools.protected_write_scope, "scope_refusal",
                        lambda op, _root, _target: f"refusing {op}: protected")
    result = harness_tools.format_code(root=str(tmp_path), tool="ruff", check_only=True)
    assert result["ok"] is False
    assert "protected" in result["error"]


@pytest.mark.parametrize("config", [
    "prettier.config.js", "prettier.config.cjs", "prettier.config.mjs", ".prettierrc.js", ".prettierrc.mjs",
])
def test_prettier_check_refuses_executable_project_config(tmp_path, no_child, not_live, config):
    (tmp_path / config).write_text("require('fs').writeFileSync('PWNED', 'x')\n", encoding="utf-8")
    result = harness_tools.format_code(root=str(tmp_path), tool="prettier", check_only=True)
    assert result["ok"] is False
    assert config in result["error"]
    assert not (tmp_path / "PWNED").exists()


@pytest.mark.parametrize("name, text", [
    (".prettierrc", '{"plugins": ["./evil.js"]}'),
    (".prettierrc.json", '{"plugins": ["./evil.js"]}'),
    (".prettierrc.yaml", "plugins:\n  - ./evil.js\n"),
    ("package.json", '{"prettier": {"plugins": ["./evil.js"]}}'),
    ("package.json", '{"prettier": "some-shared-config"}'),
])
def test_prettier_check_refuses_plugin_configs(tmp_path, no_child, not_live, name, text):
    (tmp_path / name).write_text(text, encoding="utf-8")
    result = harness_tools.format_code(root=str(tmp_path), tool="prettier", check_only=True)
    assert result["ok"] is False
    assert "check mode" in result["error"]


def test_prettier_check_refuses_a_config_in_a_parent_of_the_checked_path(tmp_path, no_child, not_live):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "prettier.config.js").write_text("module.exports = {}\n", encoding="utf-8")
    result = harness_tools.format_code(root=str(tmp_path), tool="prettier", path="pkg", check_only=True)
    assert result["ok"] is False


def test_prettier_check_runs_with_a_data_only_config(tmp_path, captured, not_live):
    (tmp_path / ".prettierrc.json").write_text('{"semi": false}', encoding="utf-8")
    (tmp_path / "package.json").write_text('{"name": "x", "prettier": {"semi": false}}', encoding="utf-8")
    result = harness_tools.format_code(root=str(tmp_path), tool="prettier", check_only=True)
    assert result["ok"] is True
    assert captured == [["npx", "--no-install", "prettier", "--check", "."]]


def test_formatter_npx_never_installs(tmp_path, captured, not_live):
    harness_tools.format_code(root=str(tmp_path), tool="prettier", check_only=False)
    harness_tools.format_code(root=str(tmp_path), tool="biome", check_only=False)
    assert all(argv[:2] == ["npx", "--no-install"] for argv in captured)


# ---- diff_files / rename / references: short names and handle checks -------

@pytest.mark.parametrize("name", ["CREDEN~1.JSO", "ENV~1", "sub/ID_RSA~1", "PROGRA~1/x.txt"])
def test_diff_files_refuses_short_name_shaped_components(workspace, name):
    (workspace / "src" / "b.txt").write_text("other\n", encoding="utf-8")
    result = harness_tools.diff_files(root=str(workspace), left=name, right="src/b.txt")
    assert result["ok"] is False
    assert "short name" in result["error"]
    assert MARK not in str(result)


def test_diff_files_diffs_the_bytes_it_read(workspace):
    (workspace / "src" / "a.txt").write_text("one\ntwo\n", encoding="utf-8")
    (workspace / "src" / "b.txt").write_text("one\n2\n", encoding="utf-8")
    result = harness_tools.diff_files(root=str(workspace), left="src/a.txt", right="src/b.txt")
    assert result["ok"] is True and result["returncode"] == 1
    assert "-two" in result["stdout"] and "+2" in result["stdout"]
    assert harness_tools.diff_files(root=str(workspace), left="src/a.txt", right="src/a.txt")["returncode"] == 0


def test_diff_files_never_hands_a_child_a_pathname(workspace, monkeypatch):
    (workspace / "src" / "a.txt").write_text("one\n", encoding="utf-8")
    (workspace / "src" / "b.txt").write_text("two\n", encoding="utf-8")
    real_run = harness_tools._run
    argvs = []

    def spy(cmd, **kwargs):
        argvs.append([str(part) for part in cmd])
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(harness_tools, "_run", spy)
    result = harness_tools.diff_files(root=str(workspace), left="src/a.txt", right="src/b.txt")
    assert result["ok"] is True and "-one" in result["stdout"] and "+two" in result["stdout"]
    assert len(argvs) == 1 and argvs[0][:3] == [sys.executable, "-I", "-c"]
    assert not any("a.txt" in part or "b.txt" in part for part in argvs[0][3:4])


def test_diff_files_is_killed_at_the_timeout_on_adversarial_input(workspace):
    # 60k short lines drawn from ~100 values stay below difflib's autojunk threshold and cost
    # it several seconds in-process (about 5 s measured), far over the 1 s timeout used here.
    import random

    generator = random.Random(1)
    left = "".join("%d\n" % generator.randrange(100) for _ in range(60000))
    right = "".join("%d\n" % generator.randrange(100) for _ in range(60000))
    (workspace / "src" / "a.txt").write_text(left, encoding="utf-8")
    (workspace / "src" / "b.txt").write_text(right, encoding="utf-8")
    started = time.time()
    result = harness_tools.diff_files(root=str(workspace), left="src/a.txt", right="src/b.txt", timeout=1)
    assert result["ok"] is False and result["timed_out"] is True
    assert "timeout" in result["error"]
    assert time.time() - started < 10


@pytest.mark.skipif(os.name != "nt", reason="pinning by sharing mode is Windows behaviour")
def test_rename_symbol_holds_the_parent_so_it_cannot_be_swapped_for_a_junction(workspace, monkeypatch):
    # While the rename holds the target's folder, renaming that folder away (the first step of
    # replacing it with a junction) must fail; the write then lands inside the root.
    target = workspace / "src" / "a.py"
    target.write_text("old = 1\n", encoding="utf-8")
    outside = workspace.parent / (workspace.name + "_outside")
    outside.mkdir()
    attempts = []
    real_stage = confined_read.PinnedDirectory.stage

    def stage_then_try_swap(self, name, text):
        temp = real_stage(self, name, text)
        try:
            os.rename(str(workspace / "src"), str(workspace / "src_moved"))
            attempts.append("renamed")
        except OSError:
            attempts.append("blocked")
        return temp

    monkeypatch.setattr(confined_read.PinnedDirectory, "stage", stage_then_try_swap)
    result = harness_tools.rename_symbol(
        root=str(workspace), old_name="old", new_name="new", glob="src/*.py", dry_run=False)
    assert attempts == ["blocked"], attempts
    assert result["ok"] is True
    assert "new = 1" in target.read_text(encoding="utf-8")
    assert not list(outside.iterdir())


def test_pinned_directory_refuses_a_directory_outside_the_root(workspace):
    outside = workspace.parent / (workspace.name + "_elsewhere")
    outside.mkdir()
    with pytest.raises(confined_read.ConfinedReadRefused):
        confined_read.PinnedDirectory(workspace, outside).__enter__()


@pytest.mark.skipif(os.name != "nt", reason="junction creation is Windows-specific here")
def test_pinned_directory_refuses_a_junction(workspace):
    outside = workspace.parent / (workspace.name + "_target")
    outside.mkdir()
    link = workspace / "src" / "hop"
    subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)], check=True, capture_output=True)
    with pytest.raises(confined_read.ConfinedReadRefused):
        confined_read.PinnedDirectory(workspace, link).__enter__()
    assert not list(outside.iterdir())


def test_diff_files_refuses_when_the_opened_handle_is_protected(workspace, monkeypatch):
    # A swap between the check and the open is modelled by making the post-open
    # classification (on the handle's final path) say protected.
    (workspace / "src" / "a.txt").write_text("one\n", encoding="utf-8")
    (workspace / "src" / "b.txt").write_text("two\n", encoding="utf-8")
    seen = []

    def protected(path):
        seen.append(path.name)
        return path.name == "b.txt"

    monkeypatch.setattr(harness_tools, "_is_protected_read_target", protected)
    result = harness_tools.diff_files(root=str(workspace), left="src/a.txt", right="src/b.txt")
    assert result["ok"] is False
    assert "refusing to diff right" in result["error"]
    assert "b.txt" in seen


def test_extract_references_and_rename_use_the_handle_checked_reader(workspace, monkeypatch):
    calls = []
    real = harness_tools.confined_read.read_confined_bytes

    def spy(root, path, **kwargs):
        calls.append(path)
        return real(root, path, **kwargs)

    monkeypatch.setattr(harness_tools.confined_read, "read_confined_bytes", spy)
    harness_tools.extract_references(root=str(workspace), symbol="key", glob="src/**/*")
    harness_tools.rename_symbol(root=str(workspace), old_name="key", new_name="k2", glob="src/**/*", dry_run=True)
    assert len(calls) >= 2


def test_rename_refuses_and_references_skip_a_short_name_shaped_file(workspace):
    (workspace / "src" / "ab~1.txt").write_text("key" + chr(10), encoding="utf-8")
    result = harness_tools.rename_symbol(
        root=str(workspace), old_name="key", new_name="k2", glob="src/**/*", dry_run=True)
    assert result["ok"] is False
    assert "short name" in result["error"]
    refs = harness_tools.extract_references(root=str(workspace), symbol="key", glob="src/**/*")
    assert {ref["file"] for ref in refs["references"]} == {os.path.join("src", "ok.txt")}


# ---- review round 2: formatter configs below the checked path, bundled formatters ----------

def _guard(tool, root, target=""):
    from sonder_runtime.adapters import formatter_config_guard

    return formatter_config_guard.executable_config_error(tool, root, target)


@pytest.mark.parametrize("target", ["", ".", "src", "src/sub"])
def test_prettier_guard_checks_every_folder_under_the_checked_path(tmp_path, target):
    sub = tmp_path / "src" / "sub"
    sub.mkdir(parents=True)
    (sub / "prettier.config.js").write_text("module.exports = {}\n", encoding="utf-8")
    assert "prettier.config.js" in _guard("prettier", tmp_path, target)


def test_prettier_guard_ignores_a_sibling_folder_outside_the_checked_file(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.js").write_text("x\n", encoding="utf-8")
    (tmp_path / "other").mkdir()
    (tmp_path / "other" / "prettier.config.js").write_text("module.exports = {}\n", encoding="utf-8")
    assert _guard("prettier", tmp_path, "src/a.js") is None
    assert _guard("prettier", tmp_path, "") is not None


@pytest.mark.parametrize("name, text", [
    ("package.yaml", "name: x\nprettier: evil-shared-config\n"),
    ("package.yaml", 'name: x\n"prettier": "evil-shared-config"\n'),
    ("package.yaml", "name: x\nprettier:\n  plugins:\n    - ./evil.js\n"),
    (".prettierrc", '{"plug\\u0069ns": ["./evil.js"]}'),
    (".prettierrc.json", '{"overrides": [{"files": "*", "options": {"plug\\u0069ns": ["./evil.js"]}}]}'),
    (".prettierrc.json", '{"plugins": ["./evil.js"]}'),
    (".prettierrc.yaml", 'pl\\x75gins: ["./evil.js"]\n'),
    (".prettierrc.yaml", "plugins:\n  - ./evil.js\n"),
    (".prettierrc.json5", "{ 'plugins': ['./evil.js'] }"),
    (".prettierrc.toml", 'plugins = ["./evil.js"]\n'),
    (".prettierrc.toml", '"plug\\u0069ns" = ["./evil.js"]\n'),
    (".prettierrc.json", "{not json"),
])
def test_prettier_guard_parses_data_configs_before_trusting_keys(tmp_path, name, text):
    folder = tmp_path / "pkg"
    folder.mkdir()
    (folder / name).write_text(text, encoding="utf-8")
    assert _guard("prettier", tmp_path, "pkg") is not None, (name, text)


@pytest.mark.parametrize("name, text", [
    (".prettierrc", '{"semi": false}'),
    (".prettierrc.yaml", '{"semi": false}'),
    (".prettierrc.toml", "semi = false\n"),
    ("package.json", '{"name": "x", "prettier": {"semi": false}}'),
    ("package.yaml", "name: x\ndependencies:\n  prettier: 3.0.0\n"),
])
def test_prettier_guard_allows_plain_option_configs(tmp_path, name, text):
    (tmp_path / name).write_text(text, encoding="utf-8")
    assert _guard("prettier", tmp_path, "") is None, name


@pytest.mark.parametrize("relative", [
    "node_modules/.bin/prettier", "node_modules/.bin/prettier.cmd", "node_modules/prettier/package.json",
])
def test_prettier_guard_refuses_a_project_shipped_formatter(tmp_path, relative):
    target = tmp_path / relative
    target.parent.mkdir(parents=True)
    target.write_text("#!/bin/sh\n", encoding="utf-8")
    assert "ships its own prettier" in _guard("prettier", tmp_path, "")


def test_biome_guard_refuses_a_project_shipped_formatter_and_plugin_configs(tmp_path):
    assert _guard("biome", tmp_path, "") is None
    (tmp_path / "biome.json").write_text('{"plugins": ["./x.grit"]}', encoding="utf-8")
    assert "biome.json" in _guard("biome", tmp_path, "")
    (tmp_path / "biome.json").write_text("{}", encoding="utf-8")
    bin_dir = tmp_path / "node_modules" / ".bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "biome").write_text("x", encoding="utf-8")
    assert "ships its own biome" in _guard("biome", tmp_path, "")


@pytest.mark.skipif(os.name != "nt", reason="junction creation is Windows-specific here")
def test_prettier_guard_refuses_a_linked_folder(tmp_path):
    target = tmp_path / "elsewhere"
    target.mkdir()
    (tmp_path / "root").mkdir()
    subprocess.run(["cmd", "/c", "mklink", "/J", str(tmp_path / "root" / "hop"), str(target)],
                   check=True, capture_output=True)
    assert "linked folder" in _guard("prettier", tmp_path / "root", "")


def test_format_check_runs_the_formatter_when_nothing_executable_is_present(tmp_path, captured, not_live):
    (tmp_path / "a.js").write_text("x\n", encoding="utf-8")
    result = harness_tools.format_code(root=str(tmp_path), tool="prettier", check_only=True)
    assert captured == [["npx", "--no-install", "prettier", "--check", "."]], result


# ---- review round 2: requirements ${VAR} and scheme-only includes ---------------------------

@pytest.mark.parametrize("line", [
    "-r ${TEMP}/x.txt",
    "-r${TEMP}/x.txt",
    "--requirement=${HOME}/x.txt",
    "requests ${EXTRA}",
    "-r file:/etc/passwd",
    "-r FILE:x.txt",
    "--constraint=file:c.txt",
    "-r https:x.txt",
])
def test_requirements_guard_refuses_variable_expansion_and_url_schemes(tmp_path, line):
    from sonder_runtime.adapters import requirements_guard

    (tmp_path / "requirements.txt").write_text(line + "\n", encoding="utf-8")
    (tmp_path / "x.txt").write_text("requests\n", encoding="utf-8")
    assert requirements_guard.requirements_content_error(tmp_path, is_protected=lambda _p: False) is not None, line


def test_requirements_guard_still_allows_plain_relative_includes(tmp_path):
    from sonder_runtime.adapters import requirements_guard

    (tmp_path / "requirements.txt").write_text("-r base.txt\nrequests==2.0\n", encoding="utf-8")
    (tmp_path / "base.txt").write_text("idna\n", encoding="utf-8")
    assert requirements_guard.requirements_content_error(tmp_path, is_protected=lambda _p: False) is None


# ---- review round 3: shared-config strings, globs, .npmrc, dotnet/cargo, archives, include secrets ----

@pytest.mark.parametrize("name,text", [
    (".prettierrc.json", '"./lib/x.cjs"'),
    (".prettierrc", "./lib/x.cjs"),
    (".prettierrc.yaml", "some-shared-config"),
    (".prettierrc.json5", '"evil-config"'),
    (".prettierrc.toml", "a = 1\n[[x"),
    (".prettierrc.json", "[]"),
])
def test_prettier_guard_refuses_non_mapping_data_configs(tmp_path, name, text):
    (tmp_path / name).write_text(text, encoding="utf-8")
    assert _guard("prettier", tmp_path, ".") is not None


def test_prettier_guard_allows_plain_json_object_config(tmp_path):
    (tmp_path / ".prettierrc").write_text('{"semi": false}', encoding="utf-8")
    assert _guard("prettier", tmp_path, ".") is None


@pytest.mark.parametrize("target", ["**/*.js", "{sub,x}", "su?", "[s]ub", "!sub", "missing", "sub/missing.js"])
def test_prettier_guard_refuses_globs_and_missing_paths(tmp_path, target):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / ".prettierrc.cjs").write_text("module.exports = {}\n", encoding="utf-8")
    assert _guard("prettier", tmp_path, target) is not None


@pytest.mark.parametrize("tool", ["prettier", "biome"])
def test_node_guards_refuse_a_project_npmrc_at_root_and_prefix(tmp_path, tool):
    (tmp_path / ".npmrc").write_text("script-shell=./tools/evil.cmd\n", encoding="utf-8")
    assert ".npmrc" in _guard(tool, tmp_path, ".")
    sub = tmp_path / "pkg" / "deep"
    sub.mkdir(parents=True)
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    assert ".npmrc" in _guard(tool, sub, ".")


@pytest.mark.parametrize("tool", ["dotnet", "rustfmt"])
def test_dotnet_and_cargo_fmt_are_refused_in_check_mode(tmp_path, tool):
    assert _guard(tool, tmp_path, ".") is not None


@pytest.mark.parametrize("line", ["evil-1.0.tar.gz", "evil.zip", "evil-1.0-py3-none-any.whl", "y.tar.bz2"])
def test_requirements_guard_refuses_bare_local_archive_names(tmp_path, line):
    from sonder_runtime.adapters import requirements_guard

    (tmp_path / "requirements.txt").write_text(line + "\n", encoding="utf-8")
    assert requirements_guard.requirements_content_error(tmp_path, is_protected=lambda _p: False) is not None


def test_requirements_include_of_a_secret_file_is_refused_without_echoing_it(tmp_path):
    (tmp_path / "requirements.txt").write_text("-r .env\n", encoding="utf-8")
    (tmp_path / ".env").write_text("OPENAI_API_KEY=sk-live-THISISASECRET0123456789\n", encoding="utf-8")
    error = harness_tools._requirements_link_error(tmp_path)
    assert error is not None
    assert "THISISASECRET" not in error and "sk-live" not in error


def test_requirements_include_error_names_file_and_line_never_content(tmp_path):
    from sonder_runtime.adapters import requirements_guard

    (tmp_path / "requirements.txt").write_text("-r base.txt\n", encoding="utf-8")
    (tmp_path / "base.txt").write_text("idna\nSECRET_TOKEN=abc123\n", encoding="utf-8")
    error = requirements_guard.requirements_content_error(tmp_path, is_protected=lambda _p: False)
    assert error is not None and "base.txt line 2" in error
    assert "SECRET_TOKEN" not in error and "abc123" not in error
