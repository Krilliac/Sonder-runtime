"""Planning a structured test run: detection, executables, containment."""
from __future__ import annotations

import json
import os
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest

from sonder_runtime.adapters.testing import detection
from sonder_runtime.adapters.testing.detection import ProjectTestPlanner, test_environment
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.testing.ports import TestRunRequest
from sonder_runtime.domain.common.errors import InvalidInput
from sonder_runtime.domain.testing.runners import ReportFormat, TestRunner

pytestmark = pytest.mark.unit


@dataclass(frozen=True)
class Record:
    name: str
    path: str
    version: str = ""


class FakeLookup:
    def __init__(self, tools: dict[str, tuple[str, str]]):
        self.tools = tools
        self.asked: list[str] = []

    def lookup(self, name):
        self.asked.append(name)
        entry = self.tools.get(name)
        return None if entry is None else Record(name, entry[0], entry[1])


def _host_tools(tmp_path):
    bin_dir = tmp_path / "hostbin"
    bin_dir.mkdir(exist_ok=True)
    tools = {}
    for name, version in (("cargo", "1.80.0"), ("go", "1.22.1"), ("ctest", "3.28.3"),
                          ("cmake", "3.28.3"), ("npm", "10.2.0"), ("pnpm", "9.0.0"),
                          ("yarn", "1.22.0"), ("make", "4.3"), ("mvn", "3.9.6"),
                          ("gradle", "8.5"), ("dotnet", "8.0.100")):
        path = bin_dir / name
        path.write_text("#!/bin/sh\n")
        tools[name] = (str(path), version)
    tools["python3"] = (sys.executable, "3.12.3")
    return tools


@pytest.fixture
def allowed(tmp_path, monkeypatch):
    root = tmp_path / "allowed"
    root.mkdir()
    monkeypatch.setenv("SONDER_FILE_ROOTS", str(root))
    return root


@pytest.fixture
def planner(tmp_path):
    lookup = FakeLookup(_host_tools(tmp_path))
    state = tmp_path / "state"
    return ProjectTestPlanner(lookup, state_dir=str(state),
                              redact=lambda text: text.replace(str(tmp_path), "<tmp>"),
                              system="posix")


def _context(*roots):
    return local_owner_context(correlation_id=uuid.uuid4().hex, workspace_roots=tuple(roots))


def _plan(planner, project, **kwargs):
    return planner.plan(TestRunRequest(project=str(project), **kwargs), _context())


def _code(excinfo):
    return getattr(excinfo.value, "code", "")


def _pytest_project(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "pytest.ini").write_text("[pytest]\n")
    (root / "test_mod.py").write_text("def test_ok():\n    pass\n")
    return root


def test_pytest_uses_a_project_virtualenv_whose_symlinked_python_resolves(allowed, planner):
    project = _pytest_project(allowed / "py")
    venv = project / ".venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("home = /usr/bin\n")
    os.symlink(sys.executable, venv / "bin" / "python")
    plan = _plan(planner, project)
    assert plan.runner is TestRunner.PYTEST
    assert plan.argv[0] == str(venv / "bin" / "python")  # unresolved: activates the venv
    assert plan.argv[1:4] == ("-m", "pytest", "-q")
    assert plan.interpreter_source == "project_venv"
    assert plan.project_executable and plan.checked_executables == ()
    assert any("virtualenv" in note for note in plan.notes)
    assert plan.report_format is ReportFormat.JUNIT_XML


def test_a_virtualenv_python_pointing_nowhere_falls_back_to_the_host_python(allowed, planner):
    project = _pytest_project(allowed / "py2")
    venv = project / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("home = /usr/bin\n")
    os.symlink(project / "missing-python", venv / "bin" / "python")
    plan = _plan(planner, project)
    assert plan.argv[0] == sys.executable
    assert plan.interpreter_source == "inventory:python3"
    assert plan.checked_executables == (sys.executable,)


@pytest.mark.parametrize("script, fmt, expected_tail", [
    ("jest --ci", ReportFormat.JEST_JSON, ["--json"]),
    ("NODE_ENV=test vitest run", ReportFormat.JUNIT_XML, ["--reporter=default", "--reporter=junit"]),
    ("node ./scripts/test.js", ReportFormat.TEXT_DIGEST, []),
])
def test_package_json_test_scripts_pick_the_reporter(allowed, planner, script, fmt, expected_tail):
    project = allowed / ("js-" + fmt.value)
    project.mkdir()
    (project / "package.json").write_text(json.dumps({"name": "x", "scripts": {"test": script}}))
    plan = _plan(planner, project)
    assert plan.runner is TestRunner.NPM
    assert plan.report_format is fmt
    assert list(plan.argv[1:3]) == ["test", "--"]
    assert list(plan.argv[3:3 + len(expected_tail)]) == expected_tail


def test_yarn_and_pnpm_lockfiles_select_their_runner(allowed, planner):
    for lock, runner in (("yarn.lock", TestRunner.YARN), ("pnpm-lock.yaml", TestRunner.PNPM)):
        project = allowed / lock
        project.mkdir()
        (project / "package.json").write_text(json.dumps({"scripts": {"test": "jest"}}))
        (project / lock).write_text("")
        assert _plan(planner, project).runner is runner


def _cmake_project(root: Path) -> Path:
    root.mkdir()
    (root / "CMakeLists.txt").write_text("project(t C)\nenable_testing()\nadd_test(NAME a COMMAND t)\n")
    return root


def test_ctest_requires_a_configured_build_tree(allowed, planner):
    project = _cmake_project(allowed / "c")
    with pytest.raises(InvalidInput) as caught:
        _plan(planner, project, runner="ctest")
    assert _code(caught) == detection.CTEST_BUILD_TREE_MISSING
    (project / "build").mkdir()
    with pytest.raises(InvalidInput):
        _plan(planner, project, runner="ctest")  # a build dir without CTestTestfile
    (project / "build" / "CTestTestfile.cmake").write_text("add_test(a t)\n")
    plan = _plan(planner, project, runner="ctest")
    assert plan.argv[1:3] == ("--test-dir", "build")
    assert "--output-junit" in plan.argv


def test_ctest_uses_out_build_trees_too(allowed, planner):
    project = _cmake_project(allowed / "c2")
    tree = project / "out" / "build" / "x64-debug"
    tree.mkdir(parents=True)
    (tree / "CTestTestfile.cmake").write_text("")
    assert _plan(planner, project).argv[2] == "out/build/x64-debug"


def test_auto_picks_the_shallowest_runner_in_enum_order(allowed, planner):
    project = _pytest_project(allowed / "multi")
    (project / "Makefile").write_text("test:\n\techo ok\n")
    crate = project / "rust"
    (crate / "src").mkdir(parents=True)
    (crate / "Cargo.toml").write_text('[package]\nname = "c"\nversion = "0.1.0"\n')
    plan = _plan(planner, project)
    assert plan.runner is TestRunner.PYTEST
    assert {"pytest@.", "make@.", "cargo@rust"} <= set(plan.candidates)
    cargo = _plan(planner, project, runner="cargo")
    assert cargo.cwd == str(crate.resolve())
    assert _plan(planner, project, runner="make").argv[1:] == ("test",)


def test_a_project_outside_the_roots_is_refused(tmp_path, allowed, planner):
    outside = _pytest_project(tmp_path / "outside")
    with pytest.raises(InvalidInput) as caught:
        _plan(planner, outside)
    assert _code(caught) == detection.PROJECT_OUTSIDE_ROOTS
    # control: the same project inside the roots is planned
    assert _plan(planner, _pytest_project(allowed / "inside")).runner is TestRunner.PYTEST


def test_a_symlinked_project_root_is_refused(allowed, planner):
    real = _pytest_project(allowed / "real")
    link = allowed / "link"
    os.symlink(real, link, target_is_directory=True)
    with pytest.raises(InvalidInput) as caught:
        _plan(planner, link)
    assert _code(caught) == detection.PROJECT_OUTSIDE_ROOTS


def test_a_lane_grant_that_excludes_the_project_refuses_it(allowed, planner):
    project = _pytest_project(allowed / "granted")
    other = allowed / "other"
    other.mkdir()
    with pytest.raises(InvalidInput) as caught:
        planner.plan(TestRunRequest(project=str(project)), _context(other))
    assert _code(caught) == detection.PROJECT_OUTSIDE_ROOTS
    plan = planner.plan(TestRunRequest(project=str(project)), _context(project))
    assert plan.runner is TestRunner.PYTEST


def test_a_missing_host_runner_is_unavailable(tmp_path, allowed):
    project = allowed / "crate"
    (project / "src").mkdir(parents=True)
    (project / "Cargo.toml").write_text('[package]\nname = "c"\nversion = "0.1.0"\n')
    planner = ProjectTestPlanner(FakeLookup({}), state_dir=str(tmp_path / "s"), redact=str,
                                 system="posix")
    with pytest.raises(InvalidInput) as caught:
        planner.plan(TestRunRequest(project=str(project)), _context())
    assert _code(caught) == detection.RUNNER_UNAVAILABLE


def test_no_declared_tests_is_reported(allowed, planner):
    empty = allowed / "empty"
    empty.mkdir()
    (empty / "README.md").write_text("hi")
    with pytest.raises(InvalidInput) as caught:
        _plan(planner, empty)
    assert _code(caught) == detection.NO_RUNNER_DETECTED
    with pytest.raises(InvalidInput) as caught:
        _plan(planner, _pytest_project(allowed / "py-only"), runner="go")
    assert _code(caught) == detection.NO_RUNNER_DETECTED


def test_unittest_needs_an_explicit_request_and_python_evidence(allowed, planner):
    project = allowed / "ut"
    project.mkdir()
    (project / "test_x.py").write_text("import unittest\n")
    with pytest.raises(InvalidInput):
        _plan(planner, project)  # auto never picks unittest
    plan = _plan(planner, project, runner="unittest")
    assert plan.argv[1:] == ("-m", "unittest", "-v")
    assert plan.report_format is ReportFormat.UNITTEST_TEXT


def test_path_selectors_must_exist_inside_the_project(tmp_path, allowed, planner):
    project = _pytest_project(allowed / "sel")
    (tmp_path / "elsewhere.py").write_text("")
    os.symlink(tmp_path / "elsewhere.py", project / "escape.py")
    for raw in ("escape.py", "missing_test.py::test_x"):
        with pytest.raises(InvalidInput) as caught:
            _plan(planner, project, selector=raw)
        assert _code(caught) == "SELECTOR_ESCAPES_PROJECT"
    plan = _plan(planner, project, selector="test_mod.py::test_ok")
    assert plan.argv[-1] == "test_mod.py::test_ok"
    assert plan.selector == "test_mod.py::test_ok"


def test_pytest_workers_need_xdist_in_the_chosen_interpreter(tmp_path, allowed, planner):
    project = _pytest_project(allowed / "w")
    plan = _plan(planner, project, workers=2)
    assert plan.argv[-4:] == ("-n", "2", "--dist", "load")
    bare = tmp_path / "bare"
    (bare / "bin").mkdir(parents=True)
    fake_python = bare / "bin" / "python3"
    fake_python.write_text("#!/bin/sh\n")
    lookup = FakeLookup({"python3": (str(fake_python), "3.12")})
    no_xdist = ProjectTestPlanner(lookup, state_dir=str(tmp_path / "s2"), redact=str, system="posix")
    with pytest.raises(InvalidInput) as caught:
        no_xdist.plan(TestRunRequest(project=str(project), workers=2), _context())
    assert _code(caught) == "WORKERS_UNSUPPORTED"
    with pytest.raises(InvalidInput) as caught:
        _plan(planner, _pytest_project(allowed / "w2"), workers=2, runner="unittest")
    assert _code(caught) == "WORKERS_UNSUPPORTED"


def test_the_display_and_digest_never_name_the_state_dir(tmp_path, allowed, planner):
    project = _pytest_project(allowed / "digest")
    first = _plan(planner, project)
    second = _plan(planner, project)
    assert first.report_dir != second.report_dir
    assert first.command_digest == second.command_digest
    state = str(tmp_path / "state")
    assert not any(state in item for item in first.display_argv)
    assert "--junitxml={report}" in first.display_argv
    assert first.cwd_label.startswith("<tmp>")
    assert _plan(planner, project, selector="k:ok").command_digest != first.command_digest


def test_the_environment_is_scrubbed_and_pinned(monkeypatch):
    monkeypatch.setenv("PYTEST_ADDOPTS", "-p evil")
    monkeypatch.setenv("GIT_DIR", "/elsewhere")
    secret_name = "SONDER_" + "API_KEY"
    monkeypatch.setenv(secret_name, "x" * 30)
    environment = dict(test_environment())
    assert "PYTEST_ADDOPTS" not in environment and "GIT_DIR" not in environment
    assert secret_name not in environment
    assert environment["CI"] == "1" and environment["NO_COLOR"] == "1"
    assert environment["PYTHONDONTWRITEBYTECODE"] == "1"
    assert "PATH" in environment  # control: ordinary variables are kept


def test_windows_project_wrappers_are_refused_and_posix_ones_disclosed(tmp_path, allowed):
    project = allowed / "gr"
    project.mkdir()
    (project / "build.gradle").write_text("plugins { id 'java' }\n")
    wrapper = project / "gradlew"
    wrapper.write_text("#!/bin/sh\nexit 0\n")
    wrapper.chmod(0o755)
    posix = ProjectTestPlanner(FakeLookup({}), state_dir=str(tmp_path / "s"), redact=str, system="posix")
    plan = posix.plan(TestRunRequest(project=str(project)), _context())
    assert plan.argv[0] == str(wrapper.resolve()) or plan.argv[0] == str(wrapper)
    assert plan.project_executable and any("wrapper" in note for note in plan.notes)
    windows = ProjectTestPlanner(FakeLookup({}), state_dir=str(tmp_path / "s"), redact=str,
                                 system="Windows")
    with pytest.raises(InvalidInput) as caught:
        windows.plan(TestRunRequest(project=str(project), runner="gradle"), _context())
    assert _code(caught) == detection.RUNNER_UNAVAILABLE


def test_windows_batch_launchers_drop_unsafe_report_paths(tmp_path, allowed):
    project = allowed / "winjs"
    project.mkdir()
    (project / "package.json").write_text(json.dumps({"scripts": {"test": "jest"}}))
    npm_cmd = tmp_path / "npm.cmd"
    npm_cmd.write_text("@echo off\n")
    state = tmp_path / "state dir with spaces"
    planner = ProjectTestPlanner(FakeLookup({"npm": (str(npm_cmd), "10")}), state_dir=str(state),
                                 redact=str, system="Windows")
    plan = planner.plan(TestRunRequest(project=str(project)), _context())
    assert plan.report_format is ReportFormat.TEXT_DIGEST
    assert detection.BATCH_ARGUMENT_UNSAFE in plan.notes
    assert "--json" not in plan.argv
    safe = ProjectTestPlanner(FakeLookup({"npm": (str(npm_cmd), "10")}),
                              state_dir=str(tmp_path / "state"), redact=str, system="Windows")
    assert safe.plan(TestRunRequest(project=str(project)), _context()).report_format is ReportFormat.JEST_JSON


def test_bad_request_fields_are_refused(allowed, planner):
    project = _pytest_project(allowed / "bad")
    with pytest.raises(InvalidInput) as caught:
        _plan(planner, project, runner="tox")
    assert _code(caught) == detection.INVALID_RUNNER
    with pytest.raises(InvalidInput):
        _plan(planner, project, workers=0)
    with pytest.raises(InvalidInput):
        _plan(planner, project, selector="--junitxml=/tmp/x")


def test_the_inventory_path_is_launched_as_recorded_not_resolved(tmp_path, allowed):
    """Multiplexing launchers (rustup proxies, busybox) dispatch on their name."""
    real = tmp_path / "rustup"
    real.write_text("#!/bin/sh\n")
    proxy = tmp_path / "cargo"
    os.symlink(real, proxy)
    project = allowed / "crate2"
    (project / "src").mkdir(parents=True)
    (project / "Cargo.toml").write_text('[package]\nname = "c"\nversion = "0.1.0"\n')
    planner = ProjectTestPlanner(FakeLookup({"cargo": (str(proxy), "1.80")}),
                                 state_dir=str(tmp_path / "s"), redact=str, system="posix")
    plan = planner.plan(TestRunRequest(project=str(project)), _context())
    assert plan.argv[0] == str(proxy)
    assert plan.checked_executables == (str(proxy),)
