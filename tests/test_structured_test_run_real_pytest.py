"""Real pytest runs through the real durable process provider."""
from __future__ import annotations

import os
import textwrap
import time
from pathlib import Path

import pytest

from sonder_runtime.application.testing.ports import TestRunRequest
from sonder_runtime.domain.testing.report import TestReport
from tests.test_tools_test_runs_harness import stack  # noqa: F401 - fixture

pytestmark = [pytest.mark.integration,
              pytest.mark.skipif(os.name != "posix", reason="process-tree checks read /proc")]

MODULE = textwrap.dedent('''\
    import pytest

    @pytest.fixture
    def broken():
        raise RuntimeError("fixture exploded")

    def test_ok1():
        assert True

    def test_ok2():
        assert 1 + 1 == 2

    def test_bad():
        assert 1 == 2

    @pytest.mark.skip(reason="later")
    def test_skip():
        pass

    def test_setup_error(broken):
        pass
''')
BAD_LINE = 14  # "assert 1 == 2" above


def _project(root: Path, module: str = MODULE) -> Path:
    root.mkdir()
    (root / "pytest.ini").write_text("[pytest]\n")
    (root / "test_mod.py").write_text(module)
    return root


def _finish(stack, job_id, limit=90.0):
    context = stack.context()
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        result = stack.service.result(job_id, context, wait_seconds=5)
        if isinstance(result, TestReport):
            return result
    raise AssertionError("test run did not finish in %.0fs" % limit)


def _alive(pid: int) -> bool:
    try:
        state = Path("/proc/%d/stat" % pid).read_text().rsplit(")", 1)[1].split()[0]
    except (FileNotFoundError, IndexError, ProcessLookupError):
        return False
    return state not in {"Z", "X"}


def test_a_real_run_reports_exact_totals_failures_and_the_summary_line(stack):
    project = _project(stack.allowed / "proj")
    job = stack.service.start(TestRunRequest(project=str(project)), stack.context())
    report = _finish(stack, job)
    assert report.status == "failed" and report.exit_code == 1
    totals = report.totals
    assert (totals.passed, totals.failed, totals.skipped, totals.errors, totals.total) == (2, 1, 1, 1, 5)
    assert report.totals_source == "junit_xml" and report.totals_reliable
    bad = {item.id: item for item in report.failures}["test_mod.py::test_bad"]
    assert (bad.file, bad.line, bad.kind) == ("test_mod.py", BAD_LINE, "failure")
    assert {item.id for item in report.failures} == {"test_mod.py::test_bad", "test_mod.py::test_setup_error"}
    # the digest's final line is pytest's own summary line (tail -1)
    assert report.digest["final_line"] == "1 failed, 2 passed, 1 skipped, 1 error in " + \
        report.digest["final_line"].split(" in ", 1)[1]
    assert report.summary_line.startswith("1 failed, 2 passed, 1 skipped, 1 error in ")
    assert report.digest["failure_lines"] == [
        "FAILED test_mod.py::test_bad - assert 1 == 2",
        "ERROR test_mod.py::test_setup_error - RuntimeError: fixture exploded",
    ]
    assert report.project == str(project.resolve())
    # the cached report is served on the next read
    assert stack.service.result(job, stack.context()) == report


def test_selectors_narrow_the_run(stack):
    project = _project(stack.allowed / "sel")
    node = _finish(stack, stack.service.start(
        TestRunRequest(project=str(project), selector="test_mod.py::test_ok1"), stack.context()))
    assert node.status == "passed" and node.totals.total == 1 and node.totals.passed == 1
    keyword = _finish(stack, stack.service.start(
        TestRunRequest(project=str(project), selector="k:ok1 or ok2"), stack.context()))
    assert keyword.status == "passed" and keyword.totals.passed == 2 and keyword.totals.total == 2


def test_workers_run_under_xdist(stack):
    pytest.importorskip("xdist")
    project = _project(stack.allowed / "xdist")
    report = _finish(stack, stack.service.start(
        TestRunRequest(project=str(project), workers=2), stack.context()))
    assert "-n" in report.display_command and report.totals.total == 5
    assert report.totals.failed == 1


SLEEPER = textwrap.dedent('''\
    import os, subprocess, sys, time

    def test_sleeps():
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        with open(os.environ.get("PIDS_FILE", "pids.txt"), "w") as handle:
            handle.write("%d %d" % (os.getpid(), child.pid))
        time.sleep(120)
''')


def _pids(project: Path, limit=30.0) -> list[int]:
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        path = project / "pids.txt"
        if path.is_file() and path.read_text().strip():
            return [int(item) for item in path.read_text().split()]
        time.sleep(0.1)
    raise AssertionError("the sleeper never started")


def test_a_run_past_its_deadline_is_timed_out_and_its_tree_is_gone(stack):
    project = _project(stack.allowed / "slow", SLEEPER)
    started = time.monotonic()
    job = stack.service.start(TestRunRequest(project=str(project), timeout_seconds=10), stack.context())
    pids = _pids(project)
    assert all(_alive(pid) for pid in pids)
    report = _finish(stack, job, limit=60)
    assert report.status == "timed_out"
    assert time.monotonic() - started < 60
    deadline = time.monotonic() + 10
    while any(_alive(pid) for pid in pids) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not any(_alive(pid) for pid in pids), "the process tree outlived the deadline"


def test_an_operator_cancel_is_cancelled_with_clean_cleanup(stack):
    project = _project(stack.allowed / "cancel", SLEEPER)
    job = stack.service.start(TestRunRequest(project=str(project)), stack.context())
    pids = _pids(project)
    cleaned = stack.launcher.cancel(job, "operator cancel")
    assert cleaned is True
    view = stack.service.status(job, stack.context())
    assert view.status == "cancelled"
    report = stack.service.result(job, stack.context())
    assert report.status == "cancelled"
    deadline = time.monotonic() + 10
    while any(_alive(pid) for pid in pids) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not any(_alive(pid) for pid in pids)


def test_a_collection_error_is_reported_as_an_error_run(stack):
    project = _project(stack.allowed / "collect", "import not_a_module_anywhere\n\ndef test_x():\n    pass\n")
    report = _finish(stack, stack.service.start(TestRunRequest(project=str(project)), stack.context()))
    assert report.status == "error" and report.exit_code == 2
    assert report.totals.errors == 1
