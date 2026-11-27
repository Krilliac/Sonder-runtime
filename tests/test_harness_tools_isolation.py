"""Protected check-mode formatters and pip requirements never hand repo code to a child.

Patch-by-patch filtering of project configuration and requirement text kept
leaving bypasses, so the surface itself is removed:

- ``format_code(check_only=True)`` with Prettier never uses ``npx``. It runs a
  trusted Prettier (``SONDER_PRETTIER_EXE`` or PATH, never under the project or
  an authorized root) that reports version 3 or newer, with ``--no-config
  --no-editorconfig``, an empty ignore file and a working folder outside the
  root, and a Node environment without ``NODE_*``/``npm_config_*``. Biome check
  mode is refused. Ruff and Black run from the runtime interpreter without
  project configuration.
- ``dependency_update``/``dependency_audit`` parse every requirement with
  ``packaging`` (PEP 508), refuse URLs, archives, paths and option lines, and
  hand pip/pip-audit a flattened file written outside the root, never the
  repository's own pathname.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

import harness_tools
import pytest

from sonder_runtime.adapters import formatter_config_guard
from sonder_runtime.adapters import requirements_guard
from sonder_runtime.adapters.filesystem import file_ops


@pytest.fixture(autouse=True)
def _authorize_pytest_tmp_roots(tmp_path_factory, monkeypatch):
    monkeypatch.setenv("SONDER_FILE_ROOTS", str(tmp_path_factory.getbasetemp()))
    monkeypatch.delenv("SONDER_FILE_BYPASS", raising=False)
    monkeypatch.delenv("SONDER_PRETTIER_EXE", raising=False)


@pytest.fixture
def not_live(monkeypatch):
    monkeypatch.setattr(harness_tools, "_live_refusal", lambda operation, root: None)


@pytest.fixture
def no_child(monkeypatch):
    monkeypatch.setattr(harness_tools, "_run", lambda *a, **k: pytest.fail(f"argv reached _run: {a!r}"))


@pytest.fixture
def trusted_dir():
    """A folder outside pytest's basetemp, so outside every authorized root in these tests."""
    folder = Path(tempfile.mkdtemp(prefix="sonder-trusted-tool-"))
    yield folder
    shutil.rmtree(folder, ignore_errors=True)


def _inside(path: str | Path, root: Path) -> bool:
    candidate = Path(os.path.abspath(str(path)))
    return candidate == root or file_ops._is_inside(candidate, root)


class _Recorder:
    """Stands in for ``harness_tools._run``; answers ``--version`` with ``version``."""

    def __init__(self, version: str = "3.3.3") -> None:
        self.version = version
        self.calls: list[dict] = []
        self.files: dict[str, str] = {}

    def __call__(self, cmd, *, cwd, timeout=None, env=None, **kwargs):
        argv = [str(c) for c in cmd]
        self.calls.append({"argv": argv, "cwd": str(cwd), "env": None if env is None else dict(env)})
        # The temp files are deleted once the run returns: capture what the child would read now.
        for index, part in enumerate(argv):
            if part in ("-r", "--ignore-path", "--config") and index + 1 < len(argv):
                target = Path(argv[index + 1])
                if target.is_file():
                    self.files[part] = target.read_text(encoding="utf-8")
                    sibling = target.parent / "constraints.txt"
                    if sibling.is_file():
                        self.files["constraints"] = sibling.read_text(encoding="utf-8")
        if "--version" in argv:
            return {"ok": True, "returncode": 0, "stdout": self.version + "\n", "stderr": "", "command": argv}
        return {"ok": True, "returncode": 0, "stdout": "", "stderr": "", "command": argv}


@pytest.fixture
def recorder(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(harness_tools, "_run", rec)
    return rec


def _trusted_prettier(folder: Path) -> Path:
    exe = folder / ("prettier.cmd" if os.name == "nt" else "prettier")
    exe.write_text("@echo off\r\n" if os.name == "nt" else "#!/bin/sh\n", encoding="utf-8")
    if os.name != "nt":
        exe.chmod(0o755)
    return exe


def _hostile_repo(base: Path) -> Path:
    """A repository whose parent folder carries an executable config and its own Prettier."""
    repo = base / "repo"
    web = repo / "packages" / "web"
    web.mkdir(parents=True)
    (web / "a.js").write_text("const a = 1;\n", encoding="utf-8")
    (repo / "package.json").write_text('{"name": "repo", "workspaces": ["packages/*"]}', encoding="utf-8")
    (repo / "prettier.config.js").write_text(
        "require('fs').writeFileSync(__dirname + '/PWNED-CONFIG', 'x'); module.exports = {};\n",
        encoding="utf-8")
    (repo / ".prettierrc.json").write_text('{"parser": "./evil-parser.js", "overrides": '
                                           '[{"files": "*", "options": {"parser": "./evil.js"}}]}',
                                           encoding="utf-8")
    (repo / ".npmrc").write_text("node-options=--require ./evil.js\n", encoding="utf-8")
    bin_dir = repo / "node_modules" / ".bin"
    bin_dir.mkdir(parents=True)
    pwned = repo / "PWNED"
    (bin_dir / "prettier.cmd").write_text('@echo off\r\necho pwned> "%s"\r\n' % pwned, encoding="utf-8")
    sh = bin_dir / "prettier"
    sh.write_text("#!/bin/sh\necho pwned > '%s'\n" % pwned, encoding="utf-8")
    if os.name != "nt":
        sh.chmod(0o755)
    return web


# ---- Prettier: trusted binary, no config, no npx ---------------------------------------------

def test_prettier_check_under_a_hostile_parent_runs_only_the_trusted_prettier_without_config(
        tmp_path, recorder, not_live, trusted_dir, monkeypatch):
    web = _hostile_repo(tmp_path)
    repo = web.parent.parent
    exe = _trusted_prettier(trusted_dir)
    monkeypatch.setenv("SONDER_PRETTIER_EXE", str(exe))

    result = harness_tools.format_code(root=str(web), tool="prettier", check_only=True)

    assert result["ok"] is True, result
    assert recorder.calls, result
    for call in recorder.calls:
        assert call["argv"][0] == str(exe), call
        assert "npx" not in call["argv"]
        assert not _inside(call["cwd"], repo), call
        assert not _inside(call["cwd"], tmp_path), call
    version_call, check_call = recorder.calls
    assert version_call["argv"][1:] == ["--version"]
    argv = check_call["argv"]
    assert "--check" in argv and "--no-config" in argv and "--no-editorconfig" in argv
    ignore = Path(argv[argv.index("--ignore-path") + 1])
    assert ignore.is_absolute() and not _inside(ignore, repo)
    assert recorder.files["--ignore-path"] == ""
    assert argv[-1] == str(web)
    assert not (repo / "PWNED").exists() and not (repo / "PWNED-CONFIG").exists()


def test_prettier_check_passes_an_explicit_target_as_an_absolute_path(
        tmp_path, recorder, not_live, trusted_dir, monkeypatch):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.js").write_text("x\n", encoding="utf-8")
    monkeypatch.setenv("SONDER_PRETTIER_EXE", str(_trusted_prettier(trusted_dir)))
    harness_tools.format_code(root=str(tmp_path), tool="prettier", path="src/a.js", check_only=True)
    assert recorder.calls[-1]["argv"][-1] == str(tmp_path / "src" / "a.js")


@pytest.mark.skipif(os.name != "nt", reason="the end-to-end shims are cmd.exe batch files")
def test_prettier_check_end_to_end_never_runs_the_repository_prettier(tmp_path, not_live, trusted_dir, monkeypatch):
    web = _hostile_repo(tmp_path)
    repo = web.parent.parent
    exe = trusted_dir / "prettier.cmd"
    record = trusted_dir / "ran.txt"
    exe.write_text(
        "@echo off\r\n"
        'if "%~1"=="--version" (\r\n  echo 3.3.3\r\n  exit /b 0\r\n)\r\n'
        'echo %* > "%~dp0ran.txt"\r\n'
        'cd >> "%~dp0ran.txt"\r\n'
        "exit /b 0\r\n",
        encoding="utf-8")
    monkeypatch.setenv("SONDER_PRETTIER_EXE", str(exe))

    result = harness_tools.format_code(root=str(web), tool="prettier", check_only=True)

    assert not (repo / "PWNED").exists(), "the repository's node_modules/.bin/prettier ran"
    assert record.is_file(), result
    ran = record.read_text(encoding="utf-8", errors="replace")
    assert "--no-config" in ran and "--no-editorconfig" in ran
    assert str(repo).lower() not in ran.splitlines()[-1].lower()
    assert result["ok"] is True, result


def test_prettier_below_version_3_is_refused(tmp_path, recorder, not_live, trusted_dir, monkeypatch):
    recorder.version = "2.8.8"
    monkeypatch.setenv("SONDER_PRETTIER_EXE", str(_trusted_prettier(trusted_dir)))
    result = harness_tools.format_code(root=str(tmp_path), tool="prettier", check_only=True)
    assert result["ok"] is False
    assert "3" in result["error"] and "version" in result["error"].lower()
    assert [call["argv"][1:] for call in recorder.calls] == [["--version"]]


@pytest.mark.parametrize("answer", ["", "not a version", "v", "Prettier"])
def test_prettier_with_an_unreadable_version_is_refused(tmp_path, recorder, not_live, trusted_dir, monkeypatch,
                                                        answer):
    recorder.version = answer
    monkeypatch.setenv("SONDER_PRETTIER_EXE", str(_trusted_prettier(trusted_dir)))
    result = harness_tools.format_code(root=str(tmp_path), tool="prettier", check_only=True)
    assert result["ok"] is False
    assert len(recorder.calls) == 1


def test_prettier_check_without_a_trusted_prettier_is_refused(tmp_path, no_child, not_live, monkeypatch):
    monkeypatch.setattr(formatter_config_guard, "trusted_which", lambda *a, **k: None)
    result = harness_tools.format_code(root=str(tmp_path), tool="prettier", check_only=True)
    assert result["ok"] is False
    assert "SONDER_PRETTIER_EXE" in result["error"]


def test_a_prettier_inside_the_project_or_an_authorized_root_is_not_trusted(
        tmp_path, no_child, not_live, monkeypatch):
    web = _hostile_repo(tmp_path)
    repo = web.parent.parent
    planted = repo / "node_modules" / ".bin" / ("prettier.cmd" if os.name == "nt" else "prettier")
    # Named explicitly: inside an authorized root (pytest's basetemp), above the checked root.
    monkeypatch.setenv("SONDER_PRETTIER_EXE", str(planted))
    result = harness_tools.format_code(root=str(web), tool="prettier", check_only=True)
    assert result["ok"] is False
    # Found on PATH: same refusal.
    monkeypatch.delenv("SONDER_PRETTIER_EXE")
    monkeypatch.setenv("PATH", str(planted.parent))
    result = harness_tools.format_code(root=str(web), tool="prettier", check_only=True)
    assert result["ok"] is False
    assert not (repo / "PWNED").exists()


@pytest.mark.parametrize("value", ["prettier", "./prettier.cmd", "node_modules/.bin/prettier"])
def test_a_relative_or_bare_prettier_setting_is_refused(tmp_path, no_child, not_live, monkeypatch, value):
    monkeypatch.setenv("SONDER_PRETTIER_EXE", value)
    result = harness_tools.format_code(root=str(tmp_path), tool="prettier", check_only=True)
    assert result["ok"] is False
    assert "SONDER_PRETTIER_EXE" in result["error"]


def test_prettier_runs_with_a_sanitized_node_environment(tmp_path, recorder, not_live, trusted_dir, monkeypatch):
    monkeypatch.setenv("SONDER_PRETTIER_EXE", str(_trusted_prettier(trusted_dir)))
    monkeypatch.setenv("NODE_OPTIONS", "--require ./evil.js")
    monkeypatch.setenv("NODE_PATH", str(tmp_path))
    monkeypatch.setenv("npm_config_script_shell", "evil.cmd")
    monkeypatch.setenv("NPM_CONFIG_PREFIX", str(tmp_path))
    monkeypatch.setenv("PRETTIER_EXPERIMENTAL_CLI", "1")
    monkeypatch.setenv("PATH", os.pathsep.join([str(tmp_path / "node_modules" / ".bin"), os.environ.get("PATH", "")]))
    harness_tools.format_code(root=str(tmp_path), tool="prettier", check_only=True)
    assert len(recorder.calls) == 2
    for call in recorder.calls:
        env = call["env"]
        assert env is not None, "the formatter inherited the runtime environment"
        upper = {key.upper() for key in env}
        assert "NODE_OPTIONS" not in upper and "NODE_PATH" not in upper
        assert not any(key.startswith(("NPM_CONFIG_", "PRETTIER_")) for key in upper)
        path_value = next((value for key, value in env.items() if key.upper() == "PATH"), "")
        assert not any(entry and _inside(entry, tmp_path) for entry in path_value.split(os.pathsep))


def test_biome_check_mode_is_refused(tmp_path, no_child, not_live):
    result = harness_tools.format_code(root=str(tmp_path), tool="biome", check_only=True)
    assert result["ok"] is False
    assert "biome" in result["error"]


# ---- Python formatters: runtime interpreter, no project configuration -------------------------

def test_ruff_check_runs_isolated_from_the_runtime_interpreter(tmp_path, recorder, not_live, monkeypatch):
    monkeypatch.setattr(formatter_config_guard, "runtime_module_available", lambda name: name == "ruff")
    (tmp_path / "pyproject.toml").write_text("[tool.ruff]\n", encoding="utf-8")
    harness_tools.format_code(root=str(tmp_path), tool="ruff", check_only=True)
    argv = recorder.calls[-1]["argv"]
    assert argv[:7] == [sys.executable, "-I", "-m", "ruff", "format", "--check", "--isolated"]


def test_black_check_runs_from_the_runtime_interpreter_with_an_empty_config(
        tmp_path, recorder, not_live, monkeypatch):
    monkeypatch.setattr(formatter_config_guard, "runtime_module_available", lambda name: name == "black")
    (tmp_path / "pyproject.toml").write_text("[tool.black]\nline-length = 1\n", encoding="utf-8")
    harness_tools.format_code(root=str(tmp_path), tool="black", check_only=True)
    argv = recorder.calls[-1]["argv"]
    assert argv[:6] == [sys.executable, "-I", "-m", "black", "--check", "--config"]
    assert not _inside(argv[6], tmp_path)
    assert recorder.files["--config"] == ""


@pytest.mark.parametrize("tool", ["ruff", "black"])
def test_python_formatter_on_path_inside_the_root_is_refused(tmp_path, no_child, not_live, monkeypatch, tool):
    monkeypatch.setattr(formatter_config_guard, "runtime_module_available", lambda name: False)
    planted = tmp_path / ".venv" / "Scripts" / (tool + ".exe")
    monkeypatch.setenv("PATH", str(planted.parent))
    result = harness_tools.format_code(root=str(tmp_path), tool=tool, check_only=True)
    assert result["ok"] is False


# ---- Requirements: strict PEP 508, sanitized copy outside the root -------------------------------

def _content_error(root: Path) -> str | None:
    return requirements_guard.requirements_content_error(
        root, is_protected=lambda _p: False, parse=harness_tools._parse_requirement)


@pytest.mark.parametrize("line", [
    "evil.zip[x]",
    "EVIL.ZIP[a,b]",
    "evil.zip[x];python_version>'3'",
    "evil-1.0.tar.gz[x]",
    "evil.TGZ ; os_name == 'nt'",
    "evil.whl==1.0",
    "evil.tar.xz",
    "evil.tbz",
])
def test_archive_named_requirements_with_extras_or_markers_are_refused(tmp_path, line):
    (tmp_path / "requirements.txt").write_text(line + "\n", encoding="utf-8")
    assert _content_error(tmp_path) is not None, line


@pytest.mark.parametrize("line", [
    "requests @ https://example.invalid/requests.whl",
    "requests@https://example.invalid/r.tar.gz",
    "pkg @ file:///C:/x/pkg",
    "https://example.invalid/r.whl",
])
def test_url_requirements_are_refused(tmp_path, line):
    (tmp_path / "requirements.txt").write_text(line + "\n", encoding="utf-8")
    assert _content_error(tmp_path) is not None, line


@pytest.mark.parametrize("line", [
    "-e .", "--editable=./pkg", "-i https://example.invalid/simple", "--index-url=https://example.invalid/s",
    "--extra-index-url https://example.invalid/s", "-f ./wheels", "--find-links=./wheels",
    "--trusted-host example.invalid", "--no-index", "--use-feature=truststore", "--no-binary :all:",
    "requests --global-option=--evil", "requests --config-settings=x=y",
])
def test_option_lines_are_refused(tmp_path, line):
    (tmp_path / "requirements.txt").write_text(line + "\n", encoding="utf-8")
    assert _content_error(tmp_path) is not None, line


@pytest.mark.parametrize("line", ["requests==", "re quests", "requests ==1 junk", "requests[", "requests; ="])
def test_unparseable_requirements_are_refused(tmp_path, line):
    (tmp_path / "requirements.txt").write_text(line + "\n", encoding="utf-8")
    assert _content_error(tmp_path) is not None, line


def _fake_venv(root: Path) -> Path:
    venv = root / ".venv"
    python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    python.parent.mkdir(parents=True)
    python.write_bytes(b"EVIL-BINARY")
    site = venv / ("Lib/site-packages" if os.name == "nt" else "lib/python3.12/site-packages")
    site.mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("home = elsewhere\n", encoding="utf-8")
    return site


def _tree(root: Path) -> None:
    (root / "reqs").mkdir(parents=True)
    (root / "requirements.txt").write_text(
        "# top\n-r reqs/base.txt  # include\n-c reqs/constraints.txt\n"
        "requests[socks]>=2.31,<3 ; python_version >= '3.9'\n",
        encoding="utf-8")
    (root / "reqs" / "base.txt").write_text(
        "urllib3==2.2.1 \\\n    --hash=sha256:" + "a" * 64 + "\n--require-hashes\n", encoding="utf-8")
    (root / "reqs" / "constraints.txt").write_text("idna<4\n", encoding="utf-8")


def _assert_sanitized(rec: _Recorder, root: Path) -> None:
    assert len(rec.calls) == 1, rec.calls
    argv = rec.calls[0]["argv"]
    given = argv[argv.index("-r") + 1]
    assert Path(given).is_absolute(), argv
    assert not _inside(given, root), argv
    assert not _inside(rec.calls[0]["cwd"], root), rec.calls[0]
    assert not any(part.endswith("requirements.txt") and _inside(part, root) for part in argv)
    text = rec.files["-r"]
    lines = [line for line in text.splitlines() if line]
    assert "--require-hashes" in lines
    assert "urllib3==2.2.1 --hash=sha256:" + "a" * 64 in lines
    assert 'requests[socks]<3,>=2.31; python_version >= "3.9"' in lines
    assert "-c constraints.txt" in lines
    assert "#" not in text and "reqs/" not in text
    assert rec.files["constraints"].splitlines() == ["idna<4"]


def test_dependency_audit_hands_pip_audit_a_sanitized_copy_outside_the_root(tmp_path, recorder, monkeypatch):
    proj = tmp_path / "proj"
    _tree(proj)
    fake = str(tmp_path / "bin" / "pip-audit")
    monkeypatch.setattr(formatter_config_guard, "pip_audit_command", lambda roots: ([fake], None))
    result = harness_tools.dependency_audit(root=str(proj))
    assert result["manager"] == "pip"
    argv = recorder.calls[0]["argv"]
    assert argv[0] == fake and argv[-2:] == ["--no-deps", "--disable-pip"]
    _assert_sanitized(recorder, proj)


def test_dependency_update_hands_pip_a_sanitized_copy_outside_the_root(tmp_path, recorder, not_live):
    proj = tmp_path / "proj"
    _tree(proj)
    site = _fake_venv(proj)
    harness_tools.dependency_update(root=str(proj), packages_json="[]")
    argv = recorder.calls[0]["argv"]
    assert argv[:7] == [sys.executable, "-I", "-m", "pip", "install", "--target", str(site)]
    _assert_sanitized(recorder, proj)


def test_the_sanitized_copy_is_removed_after_the_run(tmp_path, recorder, monkeypatch):
    (tmp_path / "requirements.txt").write_text("requests==2.32.0\n", encoding="utf-8")
    fake = str(tmp_path / "bin" / "pip-audit")
    monkeypatch.setattr(formatter_config_guard, "pip_audit_command", lambda roots: ([fake], None))
    harness_tools.dependency_audit(root=str(tmp_path))
    argv = recorder.calls[0]["argv"]
    assert not Path(argv[argv.index("-r") + 1]).exists()


def test_refused_requirement_errors_never_echo_the_line(tmp_path):
    (tmp_path / "requirements.txt").write_text("requests\nSECRETVALUE.zip[x]\n", encoding="utf-8")
    error = _content_error(tmp_path)
    assert error is not None and "requirements.txt line 2" in error
    assert "SECRETVALUE" not in error


def test_requirements_are_refused_when_no_pep508_parser_is_available(tmp_path, no_child, monkeypatch):
    (tmp_path / "requirements.txt").write_text("requests==2.32.0\n", encoding="utf-8")
    fake = str(tmp_path / "bin" / "pip-audit")
    monkeypatch.setattr(formatter_config_guard, "pip_audit_command", lambda roots: ([fake], None))
    monkeypatch.setattr(harness_tools, "_PEP508Requirement", None)
    result = harness_tools.dependency_audit(root=str(tmp_path))
    assert result["ok"] is False
    assert "parsed strictly" in result["error"]


# ---- Bare-name tools never resolve to a project file (cwd, project venv on PATH) ---------------

_IS_NT = os.name == "nt"


def _plant(folder: Path, name: str) -> list[Path]:
    """Planted lookalikes of ``name`` in ``folder``: ``.exe``/``.cmd`` on Windows, an executable elsewhere."""
    folder.mkdir(parents=True, exist_ok=True)
    planted = []
    for filename in ((name + ".exe", name + ".cmd") if _IS_NT else (name,)):
        target = folder / filename
        target.write_text("@echo off\r\necho pwned> pwned.marker\r\n" if _IS_NT else "#!/bin/sh\n", encoding="utf-8")
        if not _IS_NT:
            target.chmod(0o755)
        planted.append(target)
    return planted


class _LaunchRecorder:
    """Stands in for ``subprocess.Popen``: records every launch and starts nothing."""

    def __init__(self) -> None:
        self.argvs: list[list[str]] = []
        self.envs: list[object] = []

    def __call__(self, argv, **kwargs):
        self.argvs.append([str(part) for part in argv])
        self.envs.append(kwargs.get("env"))
        raise FileNotFoundError(argv[0])


@pytest.fixture
def launches(monkeypatch):
    recorder = _LaunchRecorder()
    monkeypatch.setattr(harness_tools.subprocess, "Popen", recorder)
    return recorder


def _hostile_environment(repo: Path, trusted: Path | None, monkeypatch) -> None:
    """cwd is the repo, the cwd-first search is on, and the repo plus its venv Scripts lead PATH."""
    monkeypatch.chdir(repo)
    monkeypatch.delenv("NoDefaultCurrentDirectoryInExePath", raising=False)
    entries = [str(repo), str(repo / "venv" / "Scripts"), str(repo / "venv" / "bin")]
    if trusted is not None:
        entries.append(str(trusted))
    monkeypatch.setenv("PATH", os.pathsep.join(entries))


def _launched_inside(launches: _LaunchRecorder, repo: Path) -> list[str]:
    """First argv parts that are relative or point inside the repo."""
    bad = []
    for argv in launches.argvs:
        program = argv[0]
        if not os.path.isabs(program) or _inside(program, repo):
            bad.append(program)
    return bad


@pytest.fixture
def audit_repo(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "requirements.txt").write_text("requests==2.31.0\n", encoding="utf-8")
    for folder in (repo, repo / "venv" / "Scripts", repo / "venv" / "bin"):
        _plant(folder, "pip-audit")
    # Force the executable route: pretend the runtime itself has no pip_audit module.
    monkeypatch.setattr(formatter_config_guard, "_runtime_module_outside", lambda name, roots: False, raising=False)
    return repo


def test_dependency_audit_never_launches_a_pip_audit_planted_in_the_project(audit_repo, launches, monkeypatch):
    _hostile_environment(audit_repo, None, monkeypatch)
    result = harness_tools.dependency_audit(root=str(audit_repo))
    assert _launched_inside(launches, audit_repo) == []
    assert result["ok"] is False


def test_dependency_audit_runs_a_trusted_pip_audit_found_after_the_planted_ones(
        audit_repo, launches, trusted_dir, monkeypatch):
    trusted = _plant(trusted_dir, "pip-audit")
    _hostile_environment(audit_repo, trusted_dir, monkeypatch)
    harness_tools.dependency_audit(root=str(audit_repo))
    assert len(launches.argvs) == 1
    assert Path(launches.argvs[0][0]).parent == trusted_dir
    assert _launched_inside(launches, audit_repo) == []
    assert trusted


def test_dependency_audit_prefers_the_runtime_pip_audit_module(audit_repo, launches, monkeypatch):
    monkeypatch.setattr(formatter_config_guard, "_runtime_module_outside", lambda name, roots: name == "pip_audit")
    _hostile_environment(audit_repo, None, monkeypatch)
    harness_tools.dependency_audit(root=str(audit_repo))
    assert launches.argvs[0][:4] == [sys.executable, "-I", "-m", "pip_audit"]


def test_runtime_module_outside_ignores_a_module_in_the_forbidden_root(tmp_path, monkeypatch):
    (tmp_path / "pip_audit").mkdir()
    (tmp_path / "pip_audit" / "__init__.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(sys, "path", ["", str(tmp_path)])
    monkeypatch.chdir(tmp_path)
    assert formatter_config_guard._runtime_module_outside("pip_audit", [tmp_path]) is False


@pytest.mark.parametrize("tool, path", [("gofmt", ""), ("clang-format", "a.cpp"), ("dart", "")])
def test_check_mode_formatters_never_launch_a_binary_planted_in_the_project(
        tmp_path, launches, not_live, monkeypatch, tool, path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.cpp").write_text("int a;\n", encoding="utf-8")
    for folder in (repo, repo / "venv" / "Scripts", repo / "venv" / "bin"):
        _plant(folder, tool)
    _hostile_environment(repo, None, monkeypatch)
    result = harness_tools.format_code(root=str(repo), tool=tool, path=path, check_only=True)
    assert _launched_inside(launches, repo) == []
    assert result["ok"] is False


def test_check_mode_gofmt_runs_a_trusted_gofmt_found_after_the_planted_ones(
        tmp_path, launches, not_live, trusted_dir, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    for folder in (repo, repo / "venv" / "Scripts"):
        _plant(folder, "gofmt")
    _plant(trusted_dir, "gofmt")
    _hostile_environment(repo, trusted_dir, monkeypatch)
    harness_tools.format_code(root=str(repo), tool="gofmt", check_only=True)
    assert len(launches.argvs) == 1
    assert Path(launches.argvs[0][0]).parent == trusted_dir
    assert _launched_inside(launches, repo) == []


@pytest.mark.skipif(not _IS_NT, reason="the cwd-first search is Windows behaviour")
def test_windows_executable_refuses_a_relative_lookup_result(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _plant(repo, "gofmt")
    monkeypatch.chdir(repo)
    monkeypatch.delenv("NoDefaultCurrentDirectoryInExePath", raising=False)
    with pytest.raises(ValueError, match="relative"):
        harness_tools._windows_executable(["gofmt", "-l"])


def test_run_keeps_an_explicit_empty_environment(tmp_path, launches):
    harness_tools._run([sys.executable, "-c", "pass"], cwd=tmp_path, env={})
    assert launches.envs == [{}]


# ---- Requirements: an archive suffix hidden in a version -----------------------------------------

@pytest.mark.parametrize("line", [
    "foo==1.0+abc.zip",
    "foo==1.0+evil.tar.gz",
    "foo==1.0+EVIL.TGZ ; python_version >= '3'",
    "foo[x]==1.0+evil.whl",
])
def test_an_archive_suffix_in_the_version_is_refused(tmp_path, line):
    (tmp_path / "requirements.txt").write_text(line + "\n", encoding="utf-8")
    assert _content_error(tmp_path) is not None, line


def test_a_plain_local_version_is_still_accepted(tmp_path):
    (tmp_path / "requirements.txt").write_text("foo==1.0+abc\n", encoding="utf-8")
    assert _content_error(tmp_path) is None


# ---- Scratch folder outside every authorized root -------------------------------------------------

def test_the_scratch_folder_is_refused_inside_an_authorized_root_other_than_the_project(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    other = tmp_path / "other-authorized"
    other.mkdir()
    monkeypatch.setenv("SONDER_FILE_ROOTS", str(other))
    monkeypatch.setattr(tempfile, "tempdir", str(other))
    with harness_tools._outside_temp_dir(project, "sonder-test-") as work:
        assert work is None


def test_the_scratch_folder_is_allowed_outside_every_root(tmp_path, trusted_dir, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(trusted_dir))
    with harness_tools._outside_temp_dir(project, "sonder-test-") as work:
        assert work is not None and not _inside(work, project)


# ---- Extended, device and UNC spellings -----------------------------------------------------------

def _spellings(path: Path) -> list[str]:
    text = str(path)
    drive, rest = os.path.splitdrive(text)
    return ["\\\\?\\" + text, "\\\\.\\" + text, "\\\\localhost\\" + drive[:1] + "$" + rest]


@pytest.mark.skipif(not _IS_NT, reason="drive-letter spellings")
def test_extended_device_and_unc_spellings_of_a_path_in_the_root_are_forbidden(tmp_path):
    inside = tmp_path / "bin" / "tool.exe"
    inside.parent.mkdir()
    inside.write_text("x", encoding="utf-8")
    for spelling in _spellings(inside):
        assert formatter_config_guard._forbidden(spelling, [tmp_path]) is True, spelling
        assert formatter_config_guard._trusted_file(spelling, [tmp_path]) is False, spelling


@pytest.mark.skipif(not _IS_NT, reason="drive-letter spellings")
def test_extended_prefixes_are_refused_even_outside_the_roots(tmp_path, trusted_dir):
    outside = trusted_dir / "tool.exe"
    outside.write_text("x", encoding="utf-8")
    assert formatter_config_guard._forbidden("\\\\?\\" + str(outside), [tmp_path]) is True
    assert formatter_config_guard._forbidden(str(outside), [tmp_path]) is False


@pytest.mark.skipif(not _IS_NT, reason="drive-letter spellings")
def test_path_entries_in_extended_or_unc_spelling_are_dropped_from_the_node_environment(tmp_path, trusted_dir):
    inside = tmp_path / "bin"
    inside.mkdir()
    entries = _spellings(inside) + [str(trusted_dir)]
    env = formatter_config_guard.sanitized_node_environment({"PATH": os.pathsep.join(entries)}, [tmp_path])
    assert env["PATH"] == str(trusted_dir)


def test_path_entries_are_compared_after_resolving_links(tmp_path, trusted_dir):
    inside = tmp_path / "bin"
    inside.mkdir()
    link = trusted_dir / "innocent"
    try:
        os.symlink(inside, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("cannot create a symlink here")
    env = formatter_config_guard.sanitized_node_environment({"PATH": str(link)}, [tmp_path])
    assert env["PATH"] == ""
