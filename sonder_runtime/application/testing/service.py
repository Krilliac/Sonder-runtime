"""Run a project's tests as an owned durable job and report the result.

The service composes the planner (host-owned argv), the launcher (durable
process job with a hard deadline) and the collector (report files). It
creates no threads: waiting is a bounded ``launcher.wait`` in short slices so
cancellation of the caller's operation cancels the run.

Ownership: every job-id method requires a ``tool.test_run`` job whose
``principal_id`` matches the caller; anything else is ``NotFound``,
indistinguishable from a job that does not exist.
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Callable, Mapping

from ...domain.common.errors import CapacityExceeded, InvalidInput, NotFound
from ...domain.testing.report import (
    MAX_NOTES,
    MAX_SUMMARY_CHARS,
    TestFailure,
    TestReport,
    TestTotals,
    one_line,
    report_from_wire,
    report_to_wire,
)
from ...domain.testing.report_parsers import (
    ParsedResults,
    parse_go_test_json,
    parse_libtest_text,
    parse_unittest_text,
)
from ...domain.testing.runners import FILE_REPORT_FORMATS, ReportFormat
from ..context import OperationContext
from ..ports.jobs import JobRecord, JobStatus
from .ports import (
    JOB_ID_PREFIX,
    JOB_KIND,
    OutputSummarizer,
    TestOutputReader,
    TestReportCollector,
    TestRunLauncher,
    TestRunPlan,
    TestRunPlanner,
    TestRunRequest,
)

TEST_RUN_BUSY = "TEST_RUN_BUSY"
JOB_NOT_FOUND = "JOB_NOT_FOUND"
MAX_RUN_WAIT_SECONDS = 120
MAX_RESULT_WAIT_SECONDS = 60
OUTPUT_WINDOW_BYTES = 2_000_000
OUTPUT_HEAD_BYTES = 65_536
_JOB_ID = re.compile(r"^test-run-[0-9a-f]{32}$")
_WAIT_SLICE_SECONDS = 1.0
_TEXT_PARSERS = {
    ReportFormat.GO_JSON: parse_go_test_json,
    ReportFormat.LIBTEST_TEXT: parse_libtest_text,
    ReportFormat.UNITTEST_TEXT: parse_unittest_text,
}


@dataclass(frozen=True)
class TestRunStatusView:
    __test__ = False  # not a pytest test class

    job_id: str
    status: str
    runner: str
    elapsed_seconds: float
    command_digest: str
    display_command: tuple[str, ...]

    def to_wire(self) -> dict:
        return {
            "object": "test_run_status",
            "job_id": self.job_id,
            "status": self.status,
            "runner": self.runner,
            "elapsed_seconds": round(float(self.elapsed_seconds), 1),
            "command_digest": self.command_digest,
            "display_command": list(self.display_command),
            "next": "call test_run_result with this job_id to wait for the report",
        }


def _not_found() -> NotFound:
    error = NotFound("test run not found")
    error.code = JOB_NOT_FOUND
    return error


def _epoch_from_iso(text: str) -> float | None:
    try:
        return datetime.fromisoformat(str(text)).timestamp()
    except (TypeError, ValueError):
        return None


def _float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class TestRunService:
    __test__ = False  # not a pytest test class

    def __init__(self, planner: TestRunPlanner, launcher: TestRunLauncher,
                 collector: TestReportCollector, *, output: TestOutputReader,
                 summarize: OutputSummarizer, redact: Callable[[str], str],
                 clock: Callable[[], float], max_concurrent_per_principal: int = 2) -> None:
        if isinstance(max_concurrent_per_principal, bool) or max_concurrent_per_principal < 1:
            raise ValueError("max_concurrent_per_principal must be positive")
        self._planner = planner
        self._launcher = launcher
        self._collector = collector
        self._output = output
        self._summarize = summarize
        self._redact = redact
        self._clock = clock
        self._max_concurrent = max_concurrent_per_principal

    # -- planning and launch -------------------------------------------------

    def plan(self, request: TestRunRequest, context: OperationContext) -> TestRunPlan:
        if not isinstance(request, TestRunRequest):
            raise InvalidInput("request must be a TestRunRequest")
        return self._planner.plan(request, context)

    def start(self, request: TestRunRequest, context: OperationContext, *,
              plan: TestRunPlan | None = None) -> str:
        if context.expired or context.cancellation.cancelled:
            raise InvalidInput("the operation was cancelled or expired before the run started")
        if self._launcher.running_for(context.principal_id) >= self._max_concurrent:
            error = CapacityExceeded(
                "at most %d test runs may run at once per caller" % self._max_concurrent)
            error.code = TEST_RUN_BUSY
            raise error
        plan = plan if plan is not None else self.plan(request, context)
        job_id = JOB_ID_PREFIX + uuid.uuid4().hex
        self._launcher.start(plan, context, job_id)
        return job_id

    def run(self, request: TestRunRequest, context: OperationContext, *,
            wait_seconds: int, plan: TestRunPlan | None = None) -> TestReport | TestRunStatusView:
        job_id = self.start(request, context, plan=plan)
        wait = max(0, min(MAX_RUN_WAIT_SECONDS, int(wait_seconds or 0)))
        return self._await(job_id, context, wait, cancel_on_abort=True)

    # -- job controls ----------------------------------------------------------

    def status(self, job_id: str, context: OperationContext) -> TestRunStatusView:
        meta = self._owned(job_id, context)
        record = self._launcher.poll(job_id)
        if record is None:
            raise _not_found()
        return self._status_view(job_id, record, meta)

    def result(self, job_id: str, context: OperationContext, *,
               wait_seconds: float = 0) -> TestReport | TestRunStatusView:
        self._owned(job_id, context)
        wait = max(0.0, min(float(MAX_RESULT_WAIT_SECONDS), float(wait_seconds or 0)))
        return self._await(job_id, context, wait, cancel_on_abort=False)

    def cancel(self, job_id: str, context: OperationContext, *, reason: str) -> TestRunStatusView:
        meta = self._owned(job_id, context)
        record = self._launcher.poll(job_id)
        if record is not None and not record.is_terminal:
            self._launcher.cancel(job_id, one_line(reason or "cancelled", 120))
            record = self._launcher.poll(job_id) or record
        if record is None:
            raise _not_found()
        return self._status_view(job_id, record, meta)

    # -- internals -------------------------------------------------------------

    def _owned(self, job_id: str, context: OperationContext) -> Mapping[str, str]:
        if not isinstance(job_id, str) or not _JOB_ID.fullmatch(job_id):
            raise _not_found()
        meta = self._launcher.metadata(job_id)
        if (
            meta is None
            or meta.get("kind") != JOB_KIND
            or meta.get("principal_id") != context.principal_id
        ):
            raise _not_found()
        return meta

    def _await(self, job_id: str, context: OperationContext, wait: float, *,
               cancel_on_abort: bool) -> TestReport | TestRunStatusView:
        meta = self._launcher.metadata(job_id) or {}
        remaining_context = context.remaining_seconds
        if remaining_context is not None:
            # Leave the caller time to render; never wait past its deadline.
            wait = max(0.0, min(wait, remaining_context - 1.0))
        record, exit_code, timed_out = self._launcher.wait(job_id, 0)
        deadline = self._clock() + wait
        while not record.is_terminal:
            if context.cancellation.cancelled or context.expired:
                if cancel_on_abort:
                    self._launcher.cancel(job_id, "caller operation cancelled")
                    record, exit_code, timed_out = self._launcher.wait(job_id, 5.0)
                    if record.is_terminal:
                        break
                return self._status_view(job_id, record, meta)
            left = deadline - self._clock()
            if left <= 0:
                return self._status_view(job_id, record, meta)
            record, exit_code, timed_out = self._launcher.wait(
                job_id, min(_WAIT_SLICE_SECONDS, left))
        return self._report(job_id, record, exit_code, meta)

    def _status_view(self, job_id: str, record: JobRecord, meta: Mapping[str, str]) -> TestRunStatusView:
        started = _float(meta.get("started_at"), self._clock())
        return TestRunStatusView(
            job_id=job_id,
            status=record.status.value,
            runner=str(meta.get("runner", "")),
            elapsed_seconds=max(0.0, self._clock() - started),
            command_digest=str(meta.get("command_digest", "")),
            display_command=self._display(meta),
        )

    @staticmethod
    def _display(meta: Mapping[str, str]) -> tuple[str, ...]:
        try:
            values = json.loads(meta.get("display_argv_json", "[]"))
        except ValueError:
            return ()
        return tuple(str(item) for item in values) if isinstance(values, list) else ()

    def _redact_failure(self, failure: TestFailure) -> TestFailure:
        return TestFailure.bounded(
            self._redact(failure.id), self._redact(failure.file), failure.line, failure.kind,
            self._redact(failure.message_excerpt),
        )

    def _report(self, job_id: str, record: JobRecord, exit_code: int | None,
                meta: Mapping[str, str]) -> TestReport:
        cached = self._collector.load_cached(meta)
        if cached is not None:
            try:
                return report_from_wire(cached)
            except (KeyError, TypeError, ValueError):
                pass  # a corrupt cache is recomputed, never trusted
        if exit_code is None and isinstance(record.result, Mapping):
            code = record.result.get("exit_code")
            exit_code = code if isinstance(code, int) and not isinstance(code, bool) else None
        fmt_name = str(meta.get("report_format", ReportFormat.TEXT_DIGEST.value))
        try:
            fmt = ReportFormat(fmt_name)
        except ValueError:
            fmt = ReportFormat.TEXT_DIGEST
        notes = self._plan_notes(meta)
        window = self._output.read_output(job_id, max_bytes=OUTPUT_WINDOW_BYTES,
                                          head_bytes=OUTPUT_HEAD_BYTES)
        text = self._redact(str(getattr(window, "text", "") or ""))
        output_truncated = bool(getattr(window, "truncated", False))
        digest = dict(self._summarize(text, "test run " + job_id))
        parsed: ParsedResults | None = None
        report_truncated = False
        if fmt in FILE_REPORT_FORMATS:
            parsed, report_truncated, note = self._collector.collect(meta)
            if note:
                notes.append(note)
        elif fmt in _TEXT_PARSERS:
            candidate = _TEXT_PARSERS[fmt](text)
            if candidate.totals.total or candidate.failures:
                parsed = candidate
            report_truncated = candidate.truncated or output_truncated
        summary = digest.get("summary") if isinstance(digest.get("summary"), Mapping) else None
        summary_line = one_line(
            (summary or {}).get("line") or digest.get("final_line") or "", MAX_SUMMARY_CHARS)
        summary_totals = self._summary_totals(summary)
        if parsed is not None:
            totals = parsed.totals
            totals_source = fmt.value
            reliable = not report_truncated and self._consistent(totals, summary)
        elif summary_totals is not None:
            totals, totals_source, reliable = summary_totals, "summary_line", False
        else:
            totals, totals_source, reliable = None, "", False
        failures = tuple(self._redact_failure(item) for item in (parsed.failures if parsed else ()))
        status = self._status(record, exit_code, totals, str(meta.get("runner", "")),
                              str((summary or {}).get("status") or ""))
        started = _float(meta.get("started_at"), 0.0)
        finished = _epoch_from_iso(record.updated_at) or self._clock()
        report = TestReport(
            runner=str(meta.get("runner", "")),
            status=status,
            job_id=job_id,
            command_digest=str(meta.get("command_digest", "")),
            display_command=self._display(meta),
            project=str(meta.get("cwd_label", "")),
            selector=str(meta.get("selector", "")),
            exit_code=exit_code,
            duration_seconds=round(max(0.0, finished - started), 3) if started else 0.0,
            totals=totals,
            totals_source=totals_source,
            totals_reliable=reliable,
            failures=failures,
            failures_truncated=bool(parsed.truncated) if parsed else False,
            report_truncated=report_truncated,
            output_truncated=output_truncated,
            summary_line=summary_line,
            digest=digest,
            notes=tuple(one_line(self._redact(note), 200) for note in dict.fromkeys(notes))[:MAX_NOTES],
        )
        try:
            self._collector.store_cached(meta, report_to_wire(report))
        except OSError:
            report = replace(report, notes=(*report.notes, "report cache could not be written")[:MAX_NOTES])
        return report

    @staticmethod
    def _plan_notes(meta: Mapping[str, str]) -> list[str]:
        try:
            values = json.loads(meta.get("notes_json") or "[]")
        except ValueError:
            return []
        return [item for item in values if isinstance(item, str)] if isinstance(values, list) else []

    @staticmethod
    def _summary_totals(summary: Mapping[str, Any] | None) -> TestTotals | None:
        if not summary:
            return None
        values = {key: summary.get(key) for key in ("passed", "failed", "skipped", "errors", "total")}
        numbers = {key: value for key, value in values.items()
                   if isinstance(value, int) and not isinstance(value, bool) and value >= 0}
        if not any(key in numbers for key in ("passed", "failed", "errors", "total")):
            return None
        passed = numbers.get("passed", 0)
        failed = numbers.get("failed", 0)
        skipped = numbers.get("skipped", 0)
        errors = numbers.get("errors", 0)
        total = numbers.get("total", passed + failed + skipped + errors)
        return TestTotals(passed, failed, skipped, errors, total)

    @staticmethod
    def _consistent(totals: TestTotals, summary: Mapping[str, Any] | None) -> bool:
        if not summary:
            return True
        for key in ("passed", "failed", "skipped", "errors"):
            value = summary.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value != getattr(totals, key):
                return False
        return True

    @staticmethod
    def _status(record: JobRecord, exit_code: int | None, totals: TestTotals | None,
                runner: str, summary_status: str = "") -> str:
        if record.status is JobStatus.CANCELLED:
            return "timed_out" if "deadline" in (record.error or "").lower() else "cancelled"
        if runner == "pytest" and exit_code is not None:
            if exit_code == 5:
                return "no_tests"
            if exit_code in {2, 3, 4}:
                return "error"
        if totals is not None and totals.total == 0 and exit_code != 0:
            return "no_tests"
        if exit_code == 0 and record.status is JobStatus.SUCCEEDED:
            if totals is not None and (totals.failed or totals.errors):
                return "failed"
            if totals is not None and totals.total == 0:
                return "no_tests"
            return "passed"
        if totals is not None and (totals.failed or totals.errors):
            return "failed"
        if totals is None and summary_status == "failed":
            return "failed"
        return "error"


__all__ = [
    "JOB_NOT_FOUND", "MAX_RESULT_WAIT_SECONDS", "MAX_RUN_WAIT_SECONDS", "TEST_RUN_BUSY",
    "TestRunService", "TestRunStatusView",
]
