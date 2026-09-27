"""Ports for structured test runs.

The planner turns a model's small request (runner, selector, project, bounded
knobs) into a host-owned plan; the launcher owns the durable process job; the
collector reads report files the runner wrote. None of these accept argv,
environment or executable paths from a caller.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping, Protocol

from ...domain.testing.report_parsers import ParsedResults
from ...domain.testing.runners import ReportFormat, TestRunner
from ..context import OperationContext
from ..ports.jobs import JobRecord

JOB_KIND = "tool.test_run"
JOB_ID_PREFIX = "test-run-"


@dataclass(frozen=True)
class TestRunRequest:
    __test__ = False  # not a pytest test class

    project: str = "."
    runner: str = "auto"
    selector: str = ""
    timeout_seconds: int | None = None
    workers: int | None = None


@dataclass(frozen=True)
class TestRunPlan:
    __test__ = False  # not a pytest test class

    runner: TestRunner
    project_root: str
    cwd: str
    argv: tuple[str, ...]
    display_argv: tuple[str, ...]
    cwd_label: str
    command_digest: str
    report_format: ReportFormat
    report_dir: str
    timeout_seconds: int
    max_descendants: int
    memory_limit_bytes: int
    environment: tuple[tuple[str, str], ...]
    candidates: tuple[str, ...] = ()
    interpreter_source: str = ""
    notes: tuple[str, ...] = ()
    selector: str = ""
    report_file: str = ""
    report_glob: str = ""
    # Executables that must still pass the host-executable guard at launch
    # (argv[0], and the project interpreter or wrapper when one is used).
    checked_executables: tuple[str, ...] = ()
    project_executable: bool = False

    def resolved_command(self) -> dict:
        """The approval-binding view of the plan: no absolute host paths."""
        return {
            "runner": self.runner.value,
            "display_argv": list(self.display_argv),
            "cwd_label": self.cwd_label,
            "command_digest": self.command_digest,
        }


class TestRunPlanner(Protocol):
    def plan(self, request: TestRunRequest, context: OperationContext) -> TestRunPlan: ...


class TestRunLauncher(Protocol):
    def start(self, plan: TestRunPlan, context: OperationContext, job_id: str) -> None: ...

    def poll(self, job_id: str) -> JobRecord | None: ...

    def wait(self, job_id: str, timeout: float) -> tuple[JobRecord, int | None, bool]:
        """(record, exit_code, timed_out); ``timed_out`` never claims terminal."""

    def cancel(self, job_id: str, reason: str) -> bool: ...

    def metadata(self, job_id: str) -> Mapping[str, str] | None:
        """The job's metadata plus its ``kind``; None when the job is unknown."""

    def running_for(self, principal_id: str) -> int: ...


class TestReportCollector(Protocol):
    def collect(self, plan_meta: Mapping[str, str]) -> tuple[ParsedResults | None, bool, str]:
        """(parsed, report_truncated, note) from the run's report file(s)."""

    def load_cached(self, plan_meta: Mapping[str, str]) -> Mapping | None: ...

    def store_cached(self, plan_meta: Mapping[str, str], wire: Mapping) -> None: ...


class TestOutputWindow(Protocol):
    text: str
    truncated: bool


class TestOutputReader(Protocol):
    """Structural subset of the diagnostics ``JobOutputReader`` port."""

    def read_output(self, job_id: str, *, max_bytes: int = 2_000_000,
                    head_bytes: int = 65_536) -> TestOutputWindow: ...


# (text, source_label) -> an ``OutputDigest.to_wire()`` mapping.
OutputSummarizer = Callable[[str, str], Mapping[str, object]]


__all__ = [
    "JOB_ID_PREFIX", "JOB_KIND", "OutputSummarizer", "TestOutputReader", "TestOutputWindow",
    "TestReportCollector", "TestRunLauncher", "TestRunPlan", "TestRunPlanner", "TestRunRequest",
]
