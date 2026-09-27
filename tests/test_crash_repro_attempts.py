"""``/crash fix`` -> ``/test`` of its repro -> one crash-fix attempt in the strategy trace.

The metric ``crash_reproduced`` (1 while the repro still crashes, 0 once it
passes, minimized) is recorded against a real sealed ``StrategyTraceService``;
the REPL path runs ``/crash fix`` and then ``/test`` of the named repro over
fake services and checks what was recorded.
"""
from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import pytest

import sonder_runtime.interfaces.repl.repl as repl
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.debugging import crash_fix as cf
from sonder_runtime.bootstrap.debug_tools import (
    CRASH_REPRO_ATTEMPT_LIMIT,
    crash_repro_run_id,
    observe_crash_repro,
)
from sonder_runtime.bootstrap.strategy import compose_strategy_trace
from sonder_runtime.domain.strategy.models import FailureClass
from sonder_runtime.interfaces.repl.facades import debug_tools as facade
from tests.test_debug_crash_fix import MAPPED, TestFailure, _report

pytestmark = pytest.mark.unit

PROJECT = "/w/game"


@dataclass(frozen=True)
class RunReport:
    __test__ = False
    runner: str
    selector: str
    status: str
    failures: tuple = ()
    failures_truncated: bool = False
    job_id: str = "test-run-0001"


CRASHED = TestFailure("game_tests", file="build/bin/game_tests",
                      message_excerpt="***Exception: SegFault  0.12 sec")


@pytest.fixture(autouse=True)
def _fresh_watch(monkeypatch):
    monkeypatch.setattr(facade, "_REPRO_WATCH", {"handoff": None, "project": ""})


@pytest.fixture
def trace(tmp_path):
    return compose_strategy_trace(db_path=tmp_path / "strategy" / "checkpoints.db",
                                  key_path=tmp_path / "strategy-private" / "checkpoint.key")


def _handoff(selector="game_tests"):
    return cf.build_crash_fix_handoff(
        _report(MAPPED, process="game_tests"),
        repro_lookup=cf.repro_lookup_for(explicit=selector),
    )


# --- what a test run says about the crash -------------------------------------------------


@pytest.mark.parametrize("report, expected", [
    (RunReport("ctest", "game_tests", "failed", (CRASHED,)), True),
    (RunReport("ctest", "game_tests", "passed"), False),
    (RunReport("ctest", "game_tests", "failed",
               (TestFailure("game_tests", message_excerpt="Failed: 2 != 3"),)), False),
    (RunReport("ctest", "other_tests", "failed", (CRASHED,)), None),
    (RunReport("pytest", "game_tests", "passed"), None),
    (RunReport("ctest", "game_tests", "timed_out"), None),
    (RunReport("ctest", "game_tests", "no_tests"), None),
    (RunReport("ctest", "game_tests", "error"), None),
    (RunReport("ctest", "game_tests", "failed"), None),
    (RunReport("ctest", "game_tests", "failed",
               (TestFailure("x", message_excerpt="Failed"),), failures_truncated=True), None),
])
def test_only_a_finished_run_of_exactly_the_repro_is_measured(report, expected):
    assert cf.crash_reproduced_in(report, _handoff().repro) is expected
    assert cf.crash_reproduced_in(report, None) is None


# --- the strategy trace -----------------------------------------------------------------------


def test_attempts_move_the_metric_from_one_to_zero_under_one_crash_identity(trace, tmp_path):
    handoff = _handoff()
    decision, number, before = observe_crash_repro(trace, handoff, project_dir=PROJECT,
                                                   reproduced_after=True)
    assert (number, before) == (1, True) and decision is not None
    _, number, before = observe_crash_repro(trace, handoff, project_dir=PROJECT,
                                            reproduced_after=False)
    assert (number, before) == (2, True)

    run_id = crash_repro_run_id(handoff, PROJECT)
    first, second = trace.history(run_id)
    expected = cf.crash_failure_observation(handoff)
    assert first.outcome == "failed" and first.failure == expected
    assert first.failure.source_code == cf.CRASH_OBSERVATION_CODE
    assert first.failure.classification is FailureClass.TEST_FAILURE  # from the handoff
    assert first.progress_before.metrics == (cf.crash_progress_metric(True),)
    assert first.progress_after.metrics == (cf.crash_progress_metric(True),)
    assert second.outcome == "succeeded" and second.failure is None
    assert second.progress_before.metrics == (cf.crash_progress_metric(True),)
    assert second.progress_after.metrics == (cf.crash_progress_metric(False),)
    assert second.progress_after.metrics[0].direction == "minimize"
    assert first.signature == second.signature

    # A restarted console continues the same run from the trace's own history.
    restarted = compose_strategy_trace(
        db_path=tmp_path / "strategy" / "checkpoints.db",
        key_path=tmp_path / "strategy-private" / "checkpoint.key")
    _, number, before = observe_crash_repro(restarted, handoff, project_dir=PROJECT,
                                            reproduced_after=False)
    assert (number, before) == (3, False)
    # Another checkout's crash with the same signature is its own run.
    assert crash_repro_run_id(handoff, "/w/other") != run_id


def test_the_attempt_budget_ends_recording(trace):
    handoff = _handoff()
    for _ in range(CRASH_REPRO_ATTEMPT_LIMIT):
        assert observe_crash_repro(trace, handoff, project_dir=PROJECT,
                                   reproduced_after=True) is not None
    assert observe_crash_repro(trace, handoff, project_dir=PROJECT,
                               reproduced_after=False) is None


def test_a_handoff_without_a_repro_is_refused(trace):
    handoff = cf.build_crash_fix_handoff(_report(MAPPED))
    with pytest.raises(ValueError):
        observe_crash_repro(trace, handoff, project_dir=PROJECT, reproduced_after=False)


# --- the facade -------------------------------------------------------------------------------


def _crash_service():
    report = _report(MAPPED, process="game_tests")
    return SimpleNamespace(result=lambda run_id, context, wait_seconds=0:
                           SimpleNamespace(crash=report))


def _context(root=PROJECT):
    return local_owner_context(correlation_id="t", source="repl", workspace_roots=(root,))


def test_crash_fix_then_the_repro_run_records_an_attempt(trace):
    brief = facade.crash_fix_brief(_crash_service(), "crash-run-1", _context(),
                                   repro="game_tests")
    assert "/test ctest game_tests" in brief
    observe = lambda report: facade.crash_repro_observation(  # noqa: E731
        report, workspace_root=PROJECT, trace_getter=lambda: trace, observe=observe_crash_repro)

    assert observe(RunReport("ctest", "game_tests", "failed", (CRASHED,))) == (
        "crash repro still crashes: crash_reproduced 1 -> 1 (attempt 1)")
    assert observe(RunReport("ctest", "unrelated", "passed")) is None
    assert observe(RunReport("ctest", "game_tests", "passed")) == (
        "crash repro passes: crash_reproduced 1 -> 0 (attempt 2)")
    handoff = facade._REPRO_WATCH["handoff"]
    assert [a.outcome for a in trace.history(crash_repro_run_id(handoff, PROJECT))] == [
        "failed", "succeeded"]


def test_without_a_strategy_trace_or_a_crash_fix_nothing_is_recorded(trace):
    calls = []

    def observe(*args, **kwargs):
        calls.append(args)

    passed = RunReport("ctest", "game_tests", "passed")
    assert facade.crash_repro_observation(passed, workspace_root=PROJECT,
                                          trace_getter=lambda: trace,
                                          observe=observe) is None  # no /crash fix yet
    facade.crash_fix_brief(_crash_service(), "crash-run-1", _context(), repro="game_tests")
    assert facade.crash_repro_observation(passed, workspace_root=PROJECT,
                                          trace_getter=lambda: None,
                                          observe=observe) is None  # tracing off
    assert calls == []


def test_a_trace_fault_is_reported_not_raised():
    facade.crash_fix_brief(_crash_service(), "crash-run-1", _context(), repro="game_tests")

    def broken(*args, **kwargs):
        raise ValueError("strategy history cannot be restored safely")

    note = facade.crash_repro_observation(RunReport("ctest", "game_tests", "passed"),
                                          workspace_root=PROJECT,
                                          trace_getter=lambda: object(), observe=broken)
    assert note == "crash repro not recorded: ValueError"


# --- the console: /crash fix, then /test of the repro -------------------------------------------


def _test_report(status, failures=()):
    return SimpleNamespace(
        job_id="test-run-0001", runner="ctest", selector="game_tests", status=status,
        exit_code=0 if status == "passed" else 8, duration_seconds=0.4,
        display_command=("ctest", "-R", "^game_tests$"),
        totals=SimpleNamespace(passed=0 if failures else 1, failed=len(failures), skipped=0,
                               errors=0, total=1),
        totals_source="junit_xml", totals_reliable=True, summary_line="",
        failures=tuple(failures), failures_truncated=False, notes=(),
        output_truncated=False, report_truncated=False,
    )


class ScriptedTestRuns:
    """Each ``/test`` finishes at once with the report the test set."""

    def __init__(self, report):
        self.report = report

    def start(self, request, context, *, plan=None):
        assert (request.runner, request.selector) == ("ctest", "game_tests")
        return "test-run-0001"

    def status(self, job_id, context):
        return SimpleNamespace(job_id=job_id, status="running", runner="ctest",
                               elapsed_seconds=0.0, command_digest="d" * 64,
                               display_command=("ctest",))

    def result(self, job_id, context, *, wait_seconds=0):
        return self.report


def test_the_console_records_the_repro_run_after_crash_fix(monkeypatch, trace):
    """/test finds the crash, /crash fix names that test as the repro, and the
    next /test of it that passes is attempt 1 with crash_reproduced 1 -> 0."""
    import sonder_runtime.bootstrap.strategy as strategy

    runs = ScriptedTestRuns(_test_report("failed", (CRASHED,)))
    monkeypatch.setattr(repl, "_developer_services", lambda: SimpleNamespace(test_runs=runs))
    monkeypatch.setattr(repl, "_debug_services", _crash_service)
    monkeypatch.setattr(repl, "_crash_source_lookup", lambda workspace="": None)
    monkeypatch.setattr(repl, "_RECENT_TEST_JOBS", [])
    monkeypatch.setattr(strategy, "try_configured_strategy_trace", lambda: trace)

    out = []
    repl._test_command("ctest game_tests", PROJECT, poll_seconds=0.0, out=out.append)
    assert not any(line.startswith("crash repro") for line in out)  # nothing to watch yet

    briefs = []
    monkeypatch.setattr(repl, "_emit", briefs.append)
    repl._crash_command("fix crash-run-1", PROJECT)
    assert "/test ctest game_tests" in briefs[-1]

    runs.report = _test_report("passed")
    out = []
    repl._test_command("ctest game_tests", PROJECT, poll_seconds=0.0, out=out.append)
    assert out[-1] == "crash repro passes: crash_reproduced 1 -> 0 (attempt 1)"
    handoff = facade._REPRO_WATCH["handoff"]
    (attempt,) = trace.history(crash_repro_run_id(handoff, PROJECT))
    assert attempt.outcome == "succeeded"


def test_a_repro_run_in_another_checkout_records_nothing(monkeypatch, trace):
    """/crash fix in checkout A, then a passing /test of the same test in
    checkout B: B's pass says nothing about A's crash and is not an attempt."""
    import sonder_runtime.bootstrap.strategy as strategy

    other = "/w/other-game"
    runs = ScriptedTestRuns(_test_report("failed", (CRASHED,)))
    monkeypatch.setattr(repl, "_developer_services", lambda: SimpleNamespace(test_runs=runs))
    monkeypatch.setattr(repl, "_debug_services", _crash_service)
    monkeypatch.setattr(repl, "_crash_source_lookup", lambda workspace="": None)
    monkeypatch.setattr(repl, "_RECENT_TEST_JOBS", [])
    monkeypatch.setattr(strategy, "try_configured_strategy_trace", lambda: trace)
    repl._test_command("ctest game_tests", PROJECT, poll_seconds=0.0, out=lambda line: None)
    monkeypatch.setattr(repl, "_emit", lambda text: None)
    repl._crash_command("fix crash-run-1", PROJECT)
    handoff = facade._REPRO_WATCH["handoff"]
    assert handoff is not None

    runs.report = _test_report("passed")
    out = []
    repl._test_command("ctest game_tests", other, poll_seconds=0.0, out=out.append)
    assert not any(line.startswith("crash repro") for line in out)
    assert tuple(trace.history(crash_repro_run_id(handoff, PROJECT))) == ()
    assert tuple(trace.history(crash_repro_run_id(handoff, other))) == ()

    # The same run in the crash's own checkout is still attempt 1.
    out = []
    repl._test_command("ctest game_tests", PROJECT, poll_seconds=0.0, out=out.append)
    assert out[-1] == "crash repro passes: crash_reproduced 1 -> 0 (attempt 1)"
