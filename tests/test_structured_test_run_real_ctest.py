"""Real CTest runs: JUnit totals, a name selector, and a missing build tree."""
from __future__ import annotations

import shutil
import subprocess
import time

import pytest

from sonder_runtime.application.testing.ports import TestRunRequest
from sonder_runtime.domain.common.errors import InvalidInput
from sonder_runtime.domain.testing.report import TestReport
from tests.test_tools_test_runs_harness import stack  # noqa: F401 - fixture

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("cmake") is None or shutil.which("ctest") is None,
                       reason="cmake/ctest not installed"),
    pytest.mark.skipif(shutil.which("cc") is None and shutil.which("gcc") is None
                       and shutil.which("clang") is None, reason="no C compiler"),
]

CMAKE = """cmake_minimum_required(VERSION 3.10)
project(t C)
enable_testing()
add_executable(t t.c)
add_test(NAME pass COMMAND t 0)
add_test(NAME fail COMMAND t 1)
"""
SOURCE = '#include <stdlib.h>\nint main(int c, char **v) { return c > 1 ? atoi(v[1]) : 0; }\n'


@pytest.fixture
def project(stack):
    root = stack.allowed / "cproj"
    root.mkdir()
    (root / "CMakeLists.txt").write_text(CMAKE)
    (root / "t.c").write_text(SOURCE)
    return root


def _build(root):
    # Test setup, not the tool: configure and build with bounded time.
    subprocess.run(["cmake", "-S", ".", "-B", "build"], cwd=root, check=True, timeout=120,
                   capture_output=True)
    subprocess.run(["cmake", "--build", "build"], cwd=root, check=True, timeout=120,
                   capture_output=True)


def _finish(stack, job_id, limit=60.0):
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        result = stack.service.result(job_id, stack.context(), wait_seconds=5)
        if isinstance(result, TestReport):
            return result
    raise AssertionError("ctest run did not finish")


def test_ctest_junit_totals_and_failure_id(stack, project):
    _build(project)
    job = stack.service.start(TestRunRequest(project=str(project), runner="ctest"), stack.context())
    report = _finish(stack, job)
    assert report.totals_source == "junit_xml"
    assert (report.totals.passed, report.totals.failed, report.totals.total) == (1, 1, 2)
    assert [item.id for item in report.failures] == ["fail"]
    assert report.status == "failed" and report.exit_code not in (None, 0)
    assert "--output-junit" in report.display_command
    assert "{report}" in report.display_command


def test_a_ctest_name_selector_runs_one_test(stack, project):
    _build(project)
    job = stack.service.start(TestRunRequest(project=str(project), runner="ctest", selector="pass"),
                              stack.context())
    report = _finish(stack, job)
    assert report.status == "passed"
    assert (report.totals.passed, report.totals.total) == (1, 1)


def test_without_a_build_tree_the_run_is_refused_before_launch(stack, project):
    with pytest.raises(InvalidInput) as caught:
        stack.service.start(TestRunRequest(project=str(project), runner="ctest"), stack.context())
    assert caught.value.code == "CTEST_BUILD_TREE_MISSING"
    assert stack.launcher.running_for(stack.context().principal_id) == 0
