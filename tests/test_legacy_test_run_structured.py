"""The legacy ``test_run`` on the structured runner, with ``extra_args_json`` retired.

``server.test_run`` (the legacy MCP tool and the legacy agent's dispatch)
keeps its arguments and its result shape. Its pytest runs now go through
``TestRunService`` -- the host-owned command template, scrubbed environment,
hard deadline and process-tree cleanup -- and a raw argv never reaches any
child process.
"""
from __future__ import annotations

import os
import textwrap
from types import SimpleNamespace

import pytest

import harness_tools
import server
from sonder_runtime.application.testing.legacy_runs import (
    LEGACY_BUSY_POLL_SECONDS,
    RETIRED_EXTRA_ARGS,
    legacy_pytest_request,
    report_to_legacy,
    retired_extra_args,
    run_legacy_pytest,
)
from sonder_runtime.application.testing.service import TestRunStatusView
from sonder_runtime.domain.common.errors import InvalidInput
from sonder_runtime.domain.testing.report import TestFailure, TestReport, TestTotals
from tests.test_tools_test_runs_harness import stack  # noqa: F401 - fixture

pytestmark = pytest.mark.unit

MODULE = textwrap.dedent('''\
    def test_ok():
        assert True

    def test_bad():
        assert 1 == 2
''')


def _report(**changes):
    values = dict(
        runner="pytest", status="failed", job_id="test-run-" + "a" * 32, command_digest="d" * 64,
        display_command=("python", "-m", "pytest"), project="[WORKSPACE]/p", selector="",
        exit_code=1, duration_seconds=1.25, totals=TestTotals(1, 1, 0, 0, 2),
        totals_source="junit_xml", totals_reliable=True,
        failures=(TestFailure.bounded("t.py::test_bad", "t.py", 4, "failure", "assert 1 == 2"),),
        summary_line="1 failed, 1 passed in 0.10s",
    )
    values.update(changes)
    return TestReport(**values)


# --- the retired argument --------------------------------------------------------------------------


@pytest.mark.parametrize("value", [None, "", "[]", "  []  ", " [ ] "])
def test_an_empty_extra_args_json_is_accepted(value):
    assert retired_extra_args(value) is None


@pytest.mark.parametrize("value", ['["-p", "evil"]', '["-c", "x.ini"]', "{bad", '"--rootdir=/"', "[1]"])
def test_any_other_extra_args_json_is_refused_by_name(value):
    refusal = retired_extra_args(value)
    assert refusal["ok"] is False and refusal["error"] == RETIRED_EXTRA_ARGS
    assert refusal["error_code"] == "EXTRA_ARGS_RETIRED"


def test_the_server_tool_refuses_it_before_any_runner(monkeypatch):
    monkeypatch.setattr(harness_tools, "_run", lambda *a, **k: pytest.fail("argv reached _run"))
    monkeypatch.setattr(server, "_developer_tool_services", lambda: SimpleNamespace(
        test_runs=SimpleNamespace(run=lambda *a, **k: pytest.fail("reached the structured runner"))))
    output = server.test_run(root=".", framework="pytest", extra_args_json='["-p", "evil"]')
    assert "  ok: False" in output and "error: " + RETIRED_EXTRA_ARGS in output


def test_the_agent_help_no_longer_advertises_it():
    line = next(row for row in server.AGENT_TOOL_HELP.splitlines() if row.startswith("- test_run:"))
    assert "extra_args_json" not in line and "extra args" not in line


# --- mapping -----------------------------------------------------------------------------------


def test_legacy_arguments_map_to_one_selector(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("")
    root = str(tmp_path)
    assert legacy_pytest_request(root).selector == ""
    assert legacy_pytest_request(root, pattern="fast").selector == "k:fast"
    assert legacy_pytest_request(root, path="tests/test_a.py").selector == "tests/test_a.py"
    assert legacy_pytest_request(
        root, path=str(tmp_path / "tests" / "test_a.py")).selector == "tests/test_a.py"
    assert legacy_pytest_request(root, path=root).selector == ""
    assert legacy_pytest_request(root, path="../elsewhere.py") is None
    assert legacy_pytest_request(root, path="tests", pattern="fast") is None
    request = legacy_pytest_request(root, timeout=5)
    assert request.runner == "pytest" and request.timeout_seconds == 10


def test_a_report_keeps_the_legacy_shape_and_its_final_summary_line():
    data = report_to_legacy(_report())
    assert (data["ok"], data["returncode"], data["timed_out"], data["framework"]) == (
        False, 1, False, "pytest")
    assert data["elapsed_ms"] == 1250 and data["command"] == ["python", "-m", "pytest"]
    lines = data["stdout"].splitlines()
    assert lines[0] == "FAILED t.py::test_bad - assert 1 == 2 (t.py:4)"
    assert lines[-1] == "1 failed, 1 passed in 0.10s"  # tail -1 is still the summary
    passed = report_to_legacy(_report(status="passed", exit_code=0, failures=()))
    assert passed["ok"] is True and passed["returncode"] == 0 and "error" not in passed
    timed = report_to_legacy(_report(status="timed_out", exit_code=None, failures=()))
    assert timed["timed_out"] is True and timed["returncode"] == -1
    assert timed["error"] == "test run timed_out"


def test_the_wrapper_waits_for_the_report_and_answers_refusals_in_shape():
    view = TestRunStatusView("test-run-" + "b" * 32, "running", "pytest", 1.0, "d", ())
    calls = []

    class Runs:
        def run(self, request, context, *, wait_seconds):
            calls.append(("run", wait_seconds))
            return view

        def result(self, job_id, context, *, wait_seconds):
            calls.append(("result", job_id))
            return _report(job_id=job_id)

    data = run_legacy_pytest(Runs(), legacy_pytest_request("/w"), context=None)
    assert data["job_id"] == view.job_id and data["ok"] is False
    assert [name for name, _ in calls] == ["run", "result"]

    class Refusing:
        def run(self, request, context, *, wait_seconds):
            error = InvalidInput("selector refused")
            error.code = "INVALID_SELECTOR"
            raise error

    refused = run_legacy_pytest(Refusing(), legacy_pytest_request("/w"), context=None)
    assert refused["ok"] is False and refused["error_code"] == "INVALID_SELECTOR"
    assert refused["error"].startswith("INVALID_SELECTOR: selector refused")


def _busy():
    from sonder_runtime.domain.common.errors import CapacityExceeded

    error = CapacityExceeded("at most 2 test runs may run at once per caller")
    error.code = "TEST_RUN_BUSY"
    return error


def test_a_busy_start_queues_for_a_slot_instead_of_answering_busy():
    now = [0.0]
    attempts, pauses = [], []

    class Runs:
        def run(self, request, context, *, wait_seconds):
            attempts.append(now[0])
            if len(attempts) < 3:
                raise _busy()
            return _report(status="passed", exit_code=0)

    def pause(seconds):
        pauses.append(seconds)
        now[0] += seconds
        return False

    data = run_legacy_pytest(Runs(), legacy_pytest_request("/w", timeout=60), context=None,
                             clock=lambda: now[0], pause=pause)
    assert data["ok"] is True and data["returncode"] == 0
    assert len(attempts) == 3 and pauses == [LEGACY_BUSY_POLL_SECONDS] * 2


def test_a_start_still_busy_past_the_queue_bound_answers_busy():
    now = [0.0]

    class Runs:
        def run(self, request, context, *, wait_seconds):
            raise _busy()

    def pause(seconds):
        now[0] += seconds
        return False

    data = run_legacy_pytest(Runs(), legacy_pytest_request("/w", timeout=30), context=None,
                             clock=lambda: now[0], pause=pause)
    assert data["ok"] is False and data["error_code"] == "TEST_RUN_BUSY"
    # The queue is bounded by the run's own timeout.
    assert 30 <= now[0] < 30 + LEGACY_BUSY_POLL_SECONDS + 1


def test_a_cancelled_caller_stops_queueing_and_other_refusals_never_queue():
    calls = []

    class Busy:
        def run(self, request, context, *, wait_seconds):
            calls.append("busy")
            raise _busy()

    data = run_legacy_pytest(Busy(), legacy_pytest_request("/w"), context=None,
                             pause=lambda seconds: True)
    assert data["error_code"] == "TEST_RUN_BUSY" and calls == ["busy"]

    class Refusing:
        def run(self, request, context, *, wait_seconds):
            calls.append("refused")
            error = InvalidInput("selector refused")
            error.code = "INVALID_SELECTOR"
            raise error

    data = run_legacy_pytest(Refusing(), legacy_pytest_request("/w"), context=None,
                             pause=lambda seconds: pytest.fail("a non-busy refusal queued"))
    assert data["error_code"] == "INVALID_SELECTOR" and calls == ["busy", "refused"]


# --- routing -----------------------------------------------------------------------------------


@pytest.mark.parametrize("kwargs", [
    {"coverage": True}, {"path": "t.py", "pattern": "x"}, {"framework": "jest"},
])
def test_what_the_runner_cannot_express_keeps_the_host_built_command(tmp_path, monkeypatch, kwargs):
    monkeypatch.setattr(server, "_developer_tool_services", lambda: SimpleNamespace(
        test_runs=SimpleNamespace(run=lambda *a, **k: pytest.fail("structured runner used"))))
    called = []
    monkeypatch.setattr(server.harness_tools, "test_run", lambda **k: called.append(k) or {
        "ok": True, "returncode": 0, "framework": k.get("framework"), "stdout": "", "stderr": ""})
    server.test_run(root=str(tmp_path), **{"framework": "pytest", **kwargs})
    assert len(called) == 1


def test_no_composed_runner_keeps_the_host_built_command(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "_developer_tool_services", lambda: None)
    called = []
    monkeypatch.setattr(server.harness_tools, "test_run", lambda **k: called.append(k) or {
        "ok": True, "returncode": 0, "framework": "pytest", "stdout": "", "stderr": ""})
    server.test_run(root=str(tmp_path), framework="pytest")
    assert len(called) == 1


@pytest.mark.integration
@pytest.mark.skipif(os.name != "posix", reason="the real runner stack reads /proc")
def test_a_real_legacy_pytest_run_goes_through_the_structured_runner(stack, monkeypatch):
    project = stack.allowed / "legacy"
    project.mkdir()
    (project / "pytest.ini").write_text("[pytest]\n")
    (project / "test_mod.py").write_text(MODULE)
    monkeypatch.setattr(server, "_developer_tool_services",
                        lambda: SimpleNamespace(test_runs=stack.service))
    monkeypatch.setattr(harness_tools, "_run", lambda *a, **k: pytest.fail("legacy argv ran"))

    output = server.test_run(root=str(project), framework="pytest", timeout=60)
    assert output.startswith("test run (pytest)")
    assert "  ok: False" in output and "  returncode: 1" in output
    assert "FAILED test_mod.py::test_bad" in output
    head, _, digest = output.partition("\ndigest:\n")
    assert "1 failed, 1 passed" in digest

    output = server.test_run(root=str(project), framework="auto", pattern="test_ok", timeout=60)
    assert "  ok: True" in output and "  returncode: 0" in output
    output = server.test_run(root=str(project), path=str(project / "test_mod.py"), timeout=60)
    assert "  ok: False" in output and "FAILED test_mod.py::test_bad" in output


@pytest.mark.integration
@pytest.mark.skipif(os.name != "posix", reason="the real runner stack reads /proc")
def test_a_host_bound_project_outside_the_file_roots_keeps_the_harness_run(stack, monkeypatch):
    # The agent's dispatch authorizes its host-selected project through
    # harness_tools.authorized_root_scope, which only the harness honors. The
    # structured planner confines to the operator's file roots alone, so it
    # would refuse this project with PROJECT_OUTSIDE_ROOTS; the call must keep
    # the harness run it always had instead of turning into that refusal.
    project = stack.allowed.parent / "hostproject"
    project.mkdir()
    (project / "pytest.ini").write_text("[pytest]\n")
    (project / "test_mod.py").write_text("def test_ok():\n    assert True\n")
    monkeypatch.setattr(server, "_developer_tool_services",
                        lambda: SimpleNamespace(test_runs=stack.service))
    with harness_tools.authorized_root_scope(str(project)):
        output = server.test_run(root=str(project), framework="pytest", timeout=60)
    assert "PROJECT_OUTSIDE_ROOTS" not in output
    assert "  ok: True" in output and "  returncode: 0" in output
    assert "1 passed" in output

    # Outside the scope the harness itself refuses the same project, so the
    # fallback grants nothing the harness would not.
    output = server.test_run(root=str(project), framework="pytest", timeout=60)
    assert output.startswith("ERROR:") and "  ok: True" not in output


@pytest.mark.integration
@pytest.mark.skipif(os.name != "posix", reason="the real runner stack reads /proc")
def test_a_legacy_run_past_the_owners_two_slots_queues_and_runs(stack, monkeypatch):
    from sonder_runtime.application.testing.ports import TestRunRequest

    project = stack.allowed / "busy"
    project.mkdir()
    (project / "pytest.ini").write_text("[pytest]\n")
    (project / "test_slow.py").write_text("import time\n\ndef test_slow():\n    time.sleep(2)\n")
    (project / "test_fast.py").write_text("def test_fast():\n    assert True\n")
    monkeypatch.setattr(server, "_developer_tool_services",
                        lambda: SimpleNamespace(test_runs=stack.service))
    context = server._developer_tool_context()
    slow = TestRunRequest(project=str(project), runner="pytest", selector="test_slow.py",
                          timeout_seconds=60)
    held = [stack.service.start(slow, context) for _ in range(2)]
    with pytest.raises(Exception) as busy:
        stack.service.start(slow, context)
    assert getattr(busy.value, "code", "") == "TEST_RUN_BUSY"

    output = server.test_run(root=str(project), framework="pytest",
                             path="test_fast.py", timeout=60)
    assert "TEST_RUN_BUSY" not in output
    assert "  ok: True" in output and "1 passed" in output
    for job_id in held:
        assert stack.service.result(job_id, context, wait_seconds=30).status == "passed"
