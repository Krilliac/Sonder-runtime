"""Developer-tool hardening in ``harness_tools`` and the secret-scan adapter.

Covers option-shaped arguments, read-only format checks, isolated project pip
execution, git ref/-- handling, isolated python -m invocations, pip-audit without
pip/pyproject installs, read guards on diff_files / extract_references /
rename_symbol, Windows PATHEXT + BatBadBut defenses, bounded process-tree
timeouts, and nested .env handling in secret_scan.
"""
from __future__ import annotations

import os
import sys
import textwrap
import time

import harness_tools
import pytest

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
    ("prettier", ["npx", "prettier", "--check", "."]),
    ("biome", ["npx", "@biomejs/biome", "format", "."]),
    ("dart", ["dart", "format", "--output=none", "--set-exit-if-changed", "."]),
    ("dotnet", ["dotnet", "format", "--verify-no-changes"]),
    ("rustfmt", ["cargo", "fmt", "--check"]),
])
def test_format_check_only_keeps_the_existing_check_forms(tmp_path, captured, tool, check):
    result = harness_tools.format_code(root=str(tmp_path), tool=tool, check_only=True)
    assert result["ok"] is True
    assert captured == [check]


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

def _fake_venv(root, name=".venv"):
    venv = root / name
    python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    python.parent.mkdir(parents=True)
    python.write_bytes(b"")
    (venv / "pyvenv.cfg").write_text("home = elsewhere\n", encoding="utf-8")
    return python


_PIP_CALLS = (
    ("dependency_add", {"packages_json": '["requests"]'}),
    ("dependency_remove", {"packages_json": '["requests"]'}),
    ("dependency_update", {"packages_json": "[]"}),
)


@pytest.mark.parametrize("tool, kwargs", _PIP_CALLS)
def test_pip_never_targets_the_runtime_interpreter(tmp_path, no_child, not_live, tool, kwargs):
    (tmp_path / "requirements.txt").write_text("requests\n", encoding="utf-8")
    result = getattr(harness_tools, tool)(root=str(tmp_path), **kwargs)
    assert result["ok"] is False
    assert "virtual environment" in result["error"]


@pytest.mark.parametrize("tool, kwargs", _PIP_CALLS)
def test_pip_runs_in_the_project_venv_isolated(tmp_path, captured, not_live, tool, kwargs):
    (tmp_path / "requirements.txt").write_text("requests\n", encoding="utf-8")
    python = _fake_venv(tmp_path)
    getattr(harness_tools, tool)(root=str(tmp_path), **kwargs)
    assert len(captured) == 1
    assert captured[0][:4] == [str(python), "-I", "-m", "pip"]
    if tool in ("dependency_add", "dependency_remove"):
        assert "--" in captured[0]
    assert sys.executable not in captured[0]


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


def test_diff_files_bounds_negative_context(workspace, captured):
    other = os.path.join("src", "ok.txt")
    (workspace / "src" / "other.txt").write_text("key two\n", encoding="utf-8")
    result = harness_tools.diff_files(root=str(workspace), left=other, right="src/other.txt", context=-5)
    assert result["ok"] is True
    assert "-U0" in captured[0]


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
