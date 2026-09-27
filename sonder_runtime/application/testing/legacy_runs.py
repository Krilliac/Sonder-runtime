"""The legacy ``test_run`` tool's pytest runs, on the structured runner.

The legacy MCP/agent ``test_run`` (``server.test_run``) keeps its arguments
(``root``, ``framework``, ``path``, ``pattern``, ``timeout``) and its result
shape (``ok``, ``returncode``, ``timed_out``, ``elapsed_ms``, ``stdout``,
``stderr``, ``command``, ``cwd``, ``framework``), which its renderer and the
grounded-outcome evidence read. For pytest it now runs through
``TestRunService``: the host-owned command template, the scrubbed
environment, the hard deadline and the process-tree cleanup of every
structured run. Its raw ``extra_args_json`` argv is retired (see
``retired_extra_args``), so no caller-built flag reaches pytest.

``stdout`` is rebuilt from the report in pytest's own ``-q`` shape -- one
``FAILED``/``ERROR`` line per failure, then the runner's summary line last --
so a reader that takes the final line or greps ``FAILED``/``ERROR`` keeps
working.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Callable

from ...domain.common.errors import SonderError
from ...domain.testing.report import TestReport
from ..context import OperationContext
from .ports import TestRunRequest

RETIRED_EXTRA_ARGS = "extra_args_json retired; use path/pattern"
LEGACY_WAIT_SLICE_SECONDS = 30
# Past the run's own hard deadline, how long the wrapper keeps waiting for the
# terminal report (collection and process-tree cleanup are included).
_DEADLINE_GRACE_SECONDS = 60
# While the caller's concurrent-run slots are all taken (TEST_RUN_BUSY), how
# often the wrapper asks again, and the longest it queues before answering busy.
LEGACY_BUSY_POLL_SECONDS = 2.0
LEGACY_MAX_QUEUE_SECONDS = 600
_BUSY = "TEST_RUN_BUSY"


def retired_extra_args(extra_args_json: Any) -> dict | None:
    """The refusal for a non-empty legacy ``extra_args_json``, else None.

    Omitted, empty and ``"[]"`` keep working; anything else -- including
    malformed JSON, which the old path silently ignored -- is refused so a
    caller learns the argv it asked for did not run.
    """
    if extra_args_json is None:
        return None
    text = str(extra_args_json).strip()
    if not text:
        return None
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        value = text
    if value == []:
        return None
    return {"ok": False, "returncode": None, "timed_out": False, "elapsed_ms": 0,
            "error": RETIRED_EXTRA_ARGS, "error_code": "EXTRA_ARGS_RETIRED",
            "command": [], "stdout": "", "stderr": ""}


def legacy_pytest_request(root: str, *, path: str = "", pattern: str = "",
                          timeout: int | None = None) -> TestRunRequest | None:
    """The structured request for a legacy pytest call, or None.

    None when the call has no single-selector equivalent (both ``path`` and
    ``pattern``; a path outside ``root``), so the caller answers it on its
    own terms. ``pattern`` becomes a ``k:`` expression and ``path`` a
    project-relative node id (``root`` itself selects nothing); the
    planner's selector grammar then decides.
    """
    root_path = Path(root)
    if path and pattern:
        return None
    selector = ""
    if pattern:
        selector = "k:" + str(pattern).strip()
    elif path:
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = root_path / candidate
        try:
            relative = os.path.relpath(os.path.abspath(candidate), os.path.abspath(root_path))
        except ValueError:  # another drive on Windows
            return None
        if relative == os.pardir or relative.startswith(os.pardir + os.sep):
            return None
        if relative != os.curdir:
            selector = relative.replace(os.sep, "/")
    return TestRunRequest(project=str(root_path), runner="pytest", selector=selector,
                          timeout_seconds=_timeout(timeout))


def _timeout(value: int | None) -> int | None:
    if value is None:
        return None
    return max(10, min(1800, int(value)))


def _stdout(report: TestReport) -> str:
    lines = []
    for failure in report.failures:
        word = "ERROR" if failure.kind == "error" else "FAILED"
        where = failure.file + (":%s" % failure.line if failure.line is not None else "")
        text = "%s %s" % (word, failure.id)
        if failure.message_excerpt:
            text += " - %s" % failure.message_excerpt
        if where and where not in text:
            text += " (%s)" % where
        lines.append(text)
    if report.failures_truncated:
        lines.append("... more failures not shown")
    lines.extend("note: %s" % note for note in report.notes)
    summary = report.summary_line or str((report.digest or {}).get("final_line") or "")
    if summary:
        lines.append(summary)
    return "\n".join(lines) + ("\n" if lines else "")


def report_to_legacy(report: TestReport) -> dict:
    """A structured report in the legacy ``test_run`` result shape."""
    exit_code = report.exit_code if report.exit_code is not None else -1
    # ``returncode: -1`` is the legacy marker for "no verdict was obtained"
    # (grounded outcomes file nothing for it). A run that ended as ``error``
    # with no test results at all -- the interpreter could not import pytest,
    # the runner crashed before collecting -- is that case, not a test
    # failure; its real exit code stays in ``exit_code``.
    no_results = report.status == "error" and report.totals is None
    returncode = -1 if no_results else exit_code
    result = {
        "ok": report.status == "passed",
        "returncode": returncode,
        "exit_code": report.exit_code,
        "timed_out": report.status == "timed_out",
        "elapsed_ms": int(round(max(0.0, report.duration_seconds) * 1000)),
        "stdout": _stdout(report),
        "stderr": "",
        "command": list(report.display_command),
        "cwd": report.project,
        "framework": "pytest",
        "job_id": report.job_id,
        "status": report.status,
        "command_digest": report.command_digest,
    }
    if no_results:
        result["error"] = "the test runner produced no results (exit %s)" % report.exit_code
    elif report.status in ("cancelled", "timed_out", "error") and not report.failures:
        result["error"] = "test run %s" % report.status
    return result


def _pause(context: OperationContext | None, seconds: float) -> bool:
    """Wait ``seconds``; True when the caller's operation was cancelled."""
    cancellation = getattr(context, "cancellation", None)
    if cancellation is not None:
        return bool(cancellation.wait(seconds))
    time.sleep(seconds)
    return False


def _start(service: Any, request: TestRunRequest, context: OperationContext, *,
           clock: Callable[[], float], pause: Callable[[float], bool]):
    """``service.run``, queueing while the caller's run slots are all taken.

    The legacy tool never had a concurrency cap, and its callers (parallel
    agent runs, fleets) expect the run to happen rather than a busy answer.
    The structured runner's per-principal cap stays in force: a busy start is
    retried every ``LEGACY_BUSY_POLL_SECONDS`` until a slot frees, for at most
    the run's own timeout (``LEGACY_MAX_QUEUE_SECONDS`` without one). Past
    that, or on cancellation, the busy refusal is the answer.
    """
    queue_until = clock() + min(LEGACY_MAX_QUEUE_SECONDS,
                                request.timeout_seconds or LEGACY_MAX_QUEUE_SECONDS)
    while True:
        try:
            return service.run(request, context, wait_seconds=LEGACY_WAIT_SLICE_SECONDS)
        except SonderError as exc:
            if str(getattr(exc, "code", "") or "") != _BUSY or clock() >= queue_until:
                raise
            if context is not None and getattr(context, "expired", False):
                raise
            if pause(LEGACY_BUSY_POLL_SECONDS):
                raise


def run_legacy_pytest(service: Any, request: TestRunRequest, context: OperationContext, *,
                      clock: Callable[[], float] = time.monotonic,
                      pause: Callable[[float], bool] | None = None) -> dict:
    """Run ``request`` to its report and answer in the legacy shape.

    A start refused only because the caller's run slots are all taken queues
    for a slot (see ``_start``). Then it waits in bounded slices until the
    run is terminal; the run's own hard deadline ends it, so the loop ends
    too. A refusal (bad selector, project outside the roots, still busy past
    the queue bound) is a legacy error result, never a raise.
    """
    wait = pause if pause is not None else (lambda seconds: _pause(context, seconds))
    budget = (request.timeout_seconds or 600) + _DEADLINE_GRACE_SECONDS
    try:
        value = _start(service, request, context, clock=clock, pause=wait)
        give_up = clock() + budget
        while not isinstance(value, TestReport):
            if clock() >= give_up:
                return {"ok": False, "returncode": -1, "timed_out": True, "elapsed_ms": 0,
                        "stdout": "", "stderr": "", "command": [], "framework": "pytest",
                        "job_id": getattr(value, "job_id", ""),
                        "error": "test run did not finish within %ds" % budget}
            value = service.result(value.job_id, context, wait_seconds=LEGACY_WAIT_SLICE_SECONDS)
    except (SonderError, PermissionError) as exc:
        code = str(getattr(exc, "code", "") or type(exc).__name__)
        return {"ok": False, "returncode": None, "timed_out": False, "elapsed_ms": 0,
                "stdout": "", "stderr": "", "command": [], "framework": "pytest",
                "error_code": code, "error": "%s: %s" % (code, str(exc)[:300])}
    return report_to_legacy(value)


__all__ = [
    "LEGACY_BUSY_POLL_SECONDS", "LEGACY_MAX_QUEUE_SECONDS", "RETIRED_EXTRA_ARGS", "legacy_pytest_request", "report_to_legacy", "retired_extra_args",
    "run_legacy_pytest",
]
