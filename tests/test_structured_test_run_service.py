"""TestRunService over fake planner/launcher/collector: ownership, capacity,
reliability, caching, redaction, bounds and cancellation."""
from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass, replace

import pytest

from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports.jobs import JobIdentity, JobRecord, JobStatus
from sonder_runtime.application.testing.ports import JOB_KIND, TestRunPlan, TestRunRequest
from sonder_runtime.application.testing.service import TestRunService, TestRunStatusView
from sonder_runtime.domain.common.errors import CapacityExceeded, NotFound
from sonder_runtime.domain.testing.report import (
    TestFailure,
    TestReport,
    TestTotals,
    fit_wire,
    render_report,
)
from sonder_runtime.domain.testing.report_parsers import ParsedResults
from sonder_runtime.domain.testing.runners import ReportFormat, TestRunner

pytestmark = pytest.mark.unit


def _plan(fmt=ReportFormat.JUNIT_XML, runner=TestRunner.PYTEST):
    return TestRunPlan(
        runner=runner, project_root="/p", cwd="/p", argv=("python", "-m", "pytest"),
        display_argv=("python", "-m", "pytest"), cwd_label="[WORKSPACE]/p",
        command_digest="d" * 64, report_format=fmt, report_dir="/state/test-runs/x",
        timeout_seconds=60, max_descendants=8, memory_limit_bytes=1 << 30, environment=(),
    )


class FakePlanner:
    def __init__(self, fmt=ReportFormat.JUNIT_XML, runner=TestRunner.PYTEST):
        self.fmt, self.runner, self.calls = fmt, runner, 0

    def plan(self, request, context):
        self.calls += 1
        return _plan(self.fmt, self.runner)


class FakeLauncher:
    def __init__(self):
        self.jobs: dict[str, dict] = {}
        self.cancelled: list[str] = []
        self.lock = threading.Lock()

    def start(self, plan, context, job_id):
        self.jobs[job_id] = {
            "meta": {"kind": JOB_KIND, "job_id": job_id, "principal_id": context.principal_id,
                     "runner": plan.runner.value, "command_digest": plan.command_digest,
                     "cwd_label": plan.cwd_label, "report_dir": plan.report_dir,
                     "report_format": plan.report_format.value, "selector": plan.selector,
                     "display_argv_json": json.dumps(list(plan.display_argv)),
                     "started_at": "%.3f" % time.time()},
            "status": JobStatus.RUNNING, "exit": None, "error": "",
        }

    def finish(self, job_id, exit_code, status=JobStatus.FAILED, error=""):
        self.jobs[job_id].update(status=status, exit=exit_code, error=error)

    def _record(self, job_id):
        job = self.jobs[job_id]
        return JobRecord(JobIdentity(job_id, job["meta"]["kind"], "op", job_id), status=job["status"],
                         error=job["error"], updated_at="2099-01-01T00:00:00+00:00")

    def poll(self, job_id):
        return self._record(job_id) if job_id in self.jobs else None

    def wait(self, job_id, timeout):
        if self.jobs[job_id]["status"] is JobStatus.RUNNING and timeout:
            time.sleep(min(timeout, 0.01))
        record = self._record(job_id)
        return record, self.jobs[job_id]["exit"], not record.is_terminal

    def cancel(self, job_id, reason):
        self.cancelled.append(job_id)
        self.finish(job_id, None, JobStatus.CANCELLED, reason)
        return True

    def metadata(self, job_id):
        job = self.jobs.get(job_id)
        return None if job is None else dict(job["meta"])

    def running_for(self, principal_id):
        return sum(1 for job in self.jobs.values()
                   if job["meta"]["principal_id"] == principal_id and job["status"] is JobStatus.RUNNING)


class FakeCollector:
    def __init__(self, parsed=None, note=""):
        self.parsed, self.note = parsed, note
        self.cache: dict[str, dict] = {}

    def collect(self, meta):
        return self.parsed, False, self.note

    def load_cached(self, meta):
        return self.cache.get(meta["job_id"])

    def store_cached(self, meta, wire):
        self.cache[meta["job_id"]] = json.loads(json.dumps(wire))


@dataclass
class Window:
    text: str
    truncated: bool = False


class FakeOutput:
    def __init__(self, text=""):
        self.text = text

    def read_output(self, job_id, *, max_bytes=2_000_000, head_bytes=65_536):
        return Window(self.text)


def _summary(text, label):
    lines = [line for line in text.splitlines() if line.strip()]
    final = lines[-1] if lines else ""
    summary = None
    if final.startswith("summary:"):
        _, counts = final.split(":", 1)
        passed, failed = (int(part) for part in counts.split("/"))
        summary = {"line": final, "passed": passed, "failed": failed, "skipped": None,
                   "errors": None, "total": None, "status": "failed" if failed else "passed"}
    return {"final_line": final, "summary": summary, "tail": lines[-5:],
            "failure_lines": [line for line in lines if line.startswith("FAILED")]}


def _service(launcher=None, collector=None, output=None, planner=None, redact=lambda t: t):
    return TestRunService(
        planner or FakePlanner(), launcher or FakeLauncher(), collector or FakeCollector(),
        output=output or FakeOutput(), summarize=_summary, redact=redact, clock=time.time,
    )


def _ctx(principal=None, **kwargs):
    context = local_owner_context(correlation_id=uuid.uuid4().hex, **kwargs)
    return replace(context, principal_id=principal) if principal else context


PARSED = ParsedResults(TestTotals(2, 1, 0, 0, 3),
                       (TestFailure.bounded("t.py::test_bad", "t.py", 3, "failure", "assert 1 == 2"),))


def test_a_run_owner_gets_the_report_and_others_get_not_found():
    launcher = FakeLauncher()
    service = _service(launcher, FakeCollector(PARSED), FakeOutput("FAILED t.py::test_bad\nsummary:2/1"))
    owner = _ctx()
    view = service.run(TestRunRequest(), owner, wait_seconds=0)
    assert isinstance(view, TestRunStatusView) and view.status == "running"
    launcher.finish(view.job_id, 1)
    report = service.result(view.job_id, owner)
    assert isinstance(report, TestReport)
    assert report.status == "failed" and report.exit_code == 1
    assert report.totals == TestTotals(2, 1, 0, 0, 3) and report.totals_reliable
    assert report.summary_line == "summary:2/1"
    stranger = _ctx("account:someone-else")
    for call in (lambda: service.result(view.job_id, stranger),
                 lambda: service.status(view.job_id, stranger),
                 lambda: service.cancel(view.job_id, stranger, reason="x")):
        with pytest.raises(NotFound):
            call()
    # a job of another kind is indistinguishable from a missing one
    launcher.jobs[view.job_id]["meta"]["kind"] = "agent_lane.test"
    with pytest.raises(NotFound):
        service.result(view.job_id, owner)
    with pytest.raises(NotFound):
        service.result("test-run-" + "0" * 32, owner)
    with pytest.raises(NotFound):
        service.result("../../etc/passwd", owner)


def test_at_most_two_concurrent_runs_per_principal():
    launcher = FakeLauncher()
    service = _service(launcher)
    first = service.start(TestRunRequest(), _ctx())
    service.start(TestRunRequest(), _ctx())
    with pytest.raises(CapacityExceeded) as caught:
        service.start(TestRunRequest(), _ctx())
    assert caught.value.code == "TEST_RUN_BUSY"
    service.start(TestRunRequest(), _ctx("account:other"))  # another caller is unaffected
    launcher.finish(first, 0, JobStatus.SUCCEEDED)
    service.start(TestRunRequest(), _ctx())  # control: capacity returns


def test_totals_are_unreliable_when_the_summary_line_disagrees():
    launcher = FakeLauncher()
    service = _service(launcher, FakeCollector(PARSED), FakeOutput("summary:5/0"))
    job = service.start(TestRunRequest(), _ctx())
    launcher.finish(job, 1)
    report = service.result(job, _ctx())
    assert report.totals == PARSED.totals and not report.totals_reliable


def test_only_a_text_digest_is_never_reliable():
    launcher = FakeLauncher()
    service = _service(launcher, FakeCollector(None), FakeOutput("make: *** [test] Error 2\nsummary:3/1"),
                       planner=FakePlanner(ReportFormat.TEXT_DIGEST, TestRunner.MAKE))
    job = service.start(TestRunRequest(), _ctx())
    launcher.finish(job, 2)
    report = service.result(job, _ctx())
    assert report.totals_source == "summary_line" and not report.totals_reliable
    assert report.status == "failed"


def test_the_report_is_cached_after_the_first_collection():
    launcher = FakeLauncher()
    collector = FakeCollector(PARSED)
    service = _service(launcher, collector, FakeOutput("summary:2/1"))
    job = service.start(TestRunRequest(), _ctx())
    launcher.finish(job, 1)
    first = service.result(job, _ctx())
    assert job in collector.cache
    collector.parsed = None  # a second read must come from the cache
    second = service.result(job, _ctx())
    assert second == first


def test_secret_like_output_is_redacted_before_it_is_stored_or_returned():
    token = "sk-" + "A1b2C3d4" * 5
    from sonder_runtime.platform.logging import Redactor

    redact = Redactor().redact
    launcher = FakeLauncher()
    failure = TestFailure.bounded("t.py::test_x", "t.py", 1, "failure", "leaked " + token)
    collector = FakeCollector(ParsedResults(TestTotals(0, 1, 0, 0, 1), (failure,)))
    service = _service(launcher, collector, FakeOutput("FAILED t.py::test_x - " + token + "\nsummary:0/1"),
                       redact=redact)
    job = service.start(TestRunRequest(), _ctx())
    launcher.finish(job, 1)
    report = service.result(job, _ctx())
    assert token not in json.dumps(fit_wire(report))
    assert token not in json.dumps(collector.cache[job])
    assert token not in render_report(report)
    # control: the redactor is what removed it
    assert token in json.dumps({"x": failure.message_excerpt})


def test_fit_wire_stays_under_48000_bytes_with_many_failures():
    failures = tuple(TestFailure.bounded("t%d" % i, "f.py", i, "failure", "m" * 400) for i in range(500))
    report = TestReport(
        runner="pytest", status="failed", job_id="test-run-" + "a" * 32, command_digest="d" * 64,
        display_command=("python",), project="p", selector="", exit_code=1, duration_seconds=1.0,
        totals=TestTotals(0, 500, 0, 0, 500), totals_source="junit_xml", totals_reliable=True,
        failures=failures, summary_line="500 failed",
        digest={"tail": ["x" * 300] * 200, "failure_lines": ["y" * 300] * 200, "groups": [],
                "first_errors": []},
        notes=("n",),
    )
    payload = fit_wire(report)
    assert len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()) <= 48_000
    assert payload["failures_truncated"] and payload["output_truncated"]
    assert payload["totals"]["failed"] == 500 and payload["status"] == "failed"
    assert len(render_report(report)) <= 6000


def test_cancelling_the_callers_operation_during_the_wait_cancels_the_run():
    launcher = FakeLauncher()
    service = _service(launcher)

    class Token:
        def __init__(self):
            self.flag = False

        @property
        def cancelled(self):
            return self.flag

        def wait(self, timeout=None):
            return self.flag

    token = Token()
    context = _ctx(cancellation=token)
    threading.Timer(0.1, lambda: setattr(token, "flag", True)).start()
    result = service.run(TestRunRequest(), context, wait_seconds=10)
    assert launcher.cancelled, "the run outlived its caller's cancellation"
    assert isinstance(result, TestReport) and result.status == "cancelled"


def test_the_wait_is_bounded_and_returns_a_status_view():
    launcher = FakeLauncher()
    service = _service(launcher)
    started = time.monotonic()
    view = service.run(TestRunRequest(), _ctx(), wait_seconds=1)
    assert isinstance(view, TestRunStatusView)
    assert time.monotonic() - started < 3
    assert not launcher.cancelled  # control: a timed-out wait does not cancel
    assert view.to_wire()["next"].startswith("call test_run_result")


def test_a_deadline_cancellation_reports_timed_out():
    launcher = FakeLauncher()
    service = _service(launcher)
    job = service.start(TestRunRequest(), _ctx())
    launcher.finish(job, None, JobStatus.CANCELLED, "process deadline exceeded")
    assert service.result(job, _ctx()).status == "timed_out"
    other = service.start(TestRunRequest(), _ctx())
    service.cancel(other, _ctx(), reason="operator")
    assert service.result(other, _ctx()).status == "cancelled"


def test_pytest_exit_codes_map_to_statuses():
    status = TestRunService._status
    running = JobRecord(JobIdentity("j", JOB_KIND, "o", "j"), status=JobStatus.FAILED)
    ok = JobRecord(JobIdentity("j", JOB_KIND, "o", "j"), status=JobStatus.SUCCEEDED)
    assert status(running, 5, TestTotals(), "pytest") == "no_tests"
    assert status(running, 2, TestTotals(0, 0, 0, 1, 1), "pytest") == "error"
    assert status(running, 1, TestTotals(1, 1, 0, 0, 2), "pytest") == "failed"
    assert status(ok, 0, TestTotals(2, 0, 0, 0, 2), "pytest") == "passed"
    assert status(running, 8, TestTotals(0, 0, 0, 0, 0), "ctest") == "no_tests"
    assert status(running, 3, None, "make") == "error"
