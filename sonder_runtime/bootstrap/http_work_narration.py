"""Request-local deferred starts for narrated HTTP work admissions."""
from __future__ import annotations

import contextvars
import functools
import logging
import re
import time
from contextlib import ExitStack, contextmanager

from sonder_runtime.bootstrap.work_narration import (
    enrich_work_record, prepare_ack, work_scope,
)
from sonder_runtime.adapters.observability import activity_tracker
from sonder_runtime.application.agents import delegation_runtime
from sonder_runtime.application.chat.handoff_receipts import ChatWorkResult

_LOG = logging.getLogger(__name__)

# How long a work run may wait on one operator approval before the gate refuses
# the call.  While the run's own thread waits it holds no routed-work slot.
APPROVAL_PARK_SECONDS = 600.0

# Lane output that begins with one of these is an error, not an answer; the
# list mirrors ``autopilot_controller.FAILURE_PREFIXES`` plus the stall marker.
_FAILURE = re.compile(r"(?:ERROR|VALIDATION_FAILED|EVIDENCE_REQUIRED|STALLED|CANCELLED)\b")
# A capacity refusal is retryable and is recorded as ``busy``, never ``failed``.
_BUSY = re.compile(
    r"(?i)\b(?:capacity (?:is )?busy|worker queue is full|WorkerPoolBackpressure"
    r"|WORK_CAPACITY_EXHAUSTED|model pool: it is busy"
    r"|timed out waiting for \w+ worker capacity)\b"
)
_DELEGATING_LINKS = frozenset({"fleet", "autopilot"})


def classify_output(output) -> str:
    """The work-run status a lane's output text stands for.

    ``refused``, ``failed`` and ``busy`` are never recorded as ``returned``;
    empty or non-text output stays ``unknown``.
    """
    text = output.strip() if isinstance(output, str) else ""
    if not text:
        return "unknown"
    first = text.splitlines()[0]
    if first.lower().startswith("refused"):
        return "refused"
    if _FAILURE.match(first):
        return "busy" if _BUSY.search(first) else "failed"
    return "returned"


WORK_RUN_OUTCOMES = ("returned", "unknown", "refused", "failed", "busy", "delegated")


def classify_work_output(output, store, run_id: str) -> tuple[str, str]:
    """``(status, child_run_id)``; a fleet or Autopilot child continues under its own id."""
    status = classify_output(output)
    child = delegated_child(store, run_id) if status == "returned" else ""
    return ("delegated", child) if child else (status, "")


def delegated_child(store, run_id: str) -> str:
    """The id of the first fleet or Autopilot run this work run started, or ``""``."""
    try:
        found = store.links(run_id)
    except Exception:
        _LOG.warning("work run links unavailable for %s", run_id, exc_info=True)
        return ""
    for item in found:
        if item.get("kind") in _DELEGATING_LINKS and item.get("id"):
            return str(item["id"])
    return ""


def approval_hook(handle, store, *, wait_seconds=APPROVAL_PARK_SECONDS):
    """The ``agent()`` approval hook for one work run: park, then resume or refuse.

    The hook publishes the call id on the run (``awaiting_approvals``, keyed by
    call id), parks the run's routed-work slot when called on the run's own
    thread (another thread that inherited the hook waits without releasing it),
    and waits for the operator's decision exactly as the streamed chat loop
    does.  It returns ``"approved"`` only when the ledger holds an approval for
    this exact call; the gate then re-decides.  Approvals are not relayed to
    delegated children (decision 29): at delegation depth above zero it refuses.
    """
    from sonder_runtime.adapters.security.permission_policy import permission_policy
    from sonder_runtime.interfaces.http.chat_tool_stream import wait_for_approval

    def hook(decision, tool, arguments):
        if delegation_runtime.current_depth() > 0:
            # Decision 29: approvals are not relayed to delegated children.  The
            # gate only compares against "approved", so any other text refuses.
            return "unrelayed"
        call_id = str(getattr(decision, "call_id", "") or "")
        ledger = permission_policy.approval_ledger()
        if not call_id or ledger is None:
            return "expired"
        try:
            digest = str(ledger.resolve_call(call_id).digest or "")
        except Exception:
            digest = ""
        digest = digest or str(permission_policy.call_digest(tool, arguments or {}) or "")
        if not digest:
            return "expired"
        try:
            store.set_narration(handle.run_id, awaiting_approval={
                "call_id": call_id, "tool": tool, "since": "%.3f" % time.time(),
                "wait_seconds": int(wait_seconds),
            })
        except Exception:
            _LOG.warning("work run %s awaiting_approval not recorded", handle.run_id, exc_info=True)
        try:
            with handle.parked():
                return wait_for_approval(
                    ledger, tool, digest, call_id,
                    timeout_seconds=wait_seconds, is_aborted=handle.aborted,
                )
        finally:
            try:
                store.set_narration(handle.run_id, clear_awaiting_approval=call_id)
            except Exception:
                _LOG.warning("work run %s awaiting_approval not cleared", handle.run_id, exc_info=True)

    return hook


_ADMISSION_SPAN_OWNERS: contextvars.ContextVar[tuple[str, str]] = contextvars.ContextVar(
    "sonder_http_work_span_owners", default=("", ""),
)


def admission_scope(store, *, wait_seconds=APPROVAL_PARK_SECONDS,
                    feed_owner=None, reasoning_owner=None):
    """Capture the admission-time permission scope; return the runner's ``body_scope``.

    The (principal, session) mode scope bound while the request is admitted is
    re-entered on the run's own thread, so the body never depends on which
    context copy its thread happened to inherit.  The approval hook is bound
    for the whole body.  Activity feed and reasoning owners from the admission
    span are captured here too so deferred work threads stay isolated to their
    originating owner.
    """
    from sonder_runtime.adapters.security.permission_policy import permission_policy
    # ``_MODE_SCOPE`` has no public reader; the provider forwards the engine's.
    pair = permission_policy._MODE_SCOPE.get()
    cur_feed, cur_reasoning = activity_tracker.current_span_owners()
    captured_owners = (
        cur_feed if feed_owner is None else str(feed_owner or ""),
        cur_reasoning if reasoning_owner is None else str(reasoning_owner or ""),
    )

    def body_scope(handle):
        @contextmanager
        def scope():
            owners_token = _ADMISSION_SPAN_OWNERS.set(captured_owners)
            try:
                with ExitStack() as stack:
                    if pair is not None:
                        stack.enter_context(permission_policy.mode_scope(*pair))
                    stack.enter_context(permission_policy.agent_approval_hook(
                        approval_hook(handle, store, wait_seconds=wait_seconds)))
                    yield
            finally:
                _ADMISSION_SPAN_OWNERS.reset(owners_token)
        return scope()

    return body_scope


def narration_scope(run_id, link_run, acknowledgement="", activity_id=""):
    return work_scope(run_id, functools.partial(link_run, run_id),
                      acknowledgement=acknowledgement, activity_id=activity_id)


def work_ack(runtime, goal, worker_cap, classified, project):
    mode = "fleet" if worker_cap else str((classified or {}).get("mode", "workbench"))
    reason = "explicit bounded worker-count request" if worker_cap else str((classified or {}).get("reason", "routed execution"))
    return prepare_ack(runtime, goal, mode, project, reason,
                       agents=int(worker_cap) if worker_cap else None,
                       worker_cap=worker_cap or None)


def admission_hook(store, acknowledgement):
    def save(run_id):
        store.set_narration(run_id, acknowledgement=run_ack(acknowledgement, run_id))
    return save


def run_ack(text, run_id):
    """Bind deterministic controls after the owner-scoped run is admitted."""
    prefix, marker, _ = text.rpartition("watch ")
    return (prefix if marker else text + " ") + (
        "watch GET /v1/work-runs/%s; cancel with POST /v1/work-runs/%s/cancel."
        % (run_id, run_id))


def start_work(runner, principal, function, acknowledgement, store, *,
               classify, thread_wrapper=None, session_ref=""):
    def classify_and_capture(result):
        if isinstance(result, ChatWorkResult):
            store.set_narration(result.work_run_id, result_receipt=result.public_receipt())
        return classify(result)
    outcome = runner.run(
        principal, function, classify=classify_and_capture, thread_wrapper=thread_wrapper,
        wait_seconds=0, defer_start=defer,
        on_admitted=admission_hook(store, acknowledgement),
        body_scope=admission_scope(store),
    )
    text = run_ack(acknowledgement, outcome.run_id)
    return ChatWorkResult(text, "running", session_ref=session_ref,
                          work_run_id=outcome.run_id, acknowledgement=text)


def admission_result(result, session_ref=""):
    """Preserve the chat work seam's existing typed refusal boundary."""
    if isinstance(result, ChatWorkResult):
        return result
    return ChatWorkResult(result if isinstance(result, str) else "",
                          "refused" if isinstance(result, str) else "unknown", session_ref=session_ref)


def run_scoped(
    run_id, link_run, acknowledgement, activity_id, function,
    *, feed_owner=None, reasoning_owner=None,
):
    from sonder_runtime.application.ports.work_narration import bind_activity
    inherited_feed, inherited_reasoning = _ADMISSION_SPAN_OWNERS.get()
    if not inherited_feed and not inherited_reasoning:
        inherited_feed, inherited_reasoning = activity_tracker.current_span_owners()
    effective_feed = inherited_feed if feed_owner is None else str(feed_owner or "")
    effective_reasoning = (
        inherited_reasoning if reasoning_owner is None else str(reasoning_owner or "")
    )
    with narration_scope(run_id, link_run, acknowledgement, activity_id):
        with activity_tracker.response_span(
            "work run",
            surface="http-work",
            feed_owner=effective_feed,
            reasoning_owner=effective_reasoning,
        ):
            bind_activity(activity_tracker.current_response_id() or "")
            return function()


def deferred_post(function):
    @functools.wraps(function)
    def wrapped(self):
        with response_scope() as deferred:
            try:
                return function(self)
            finally:
                deferred.flush()
    return wrapped


class DeferredStarts:
    """Collect worker starters and run them after the HTTP response path."""

    def __init__(self):
        self._starters = []
        self._flushed = False

    def add(self, starter):
        if self._flushed:
            starter()
            return
        self._starters.append(starter)

    def flush(self):
        self._flushed = True
        pending, self._starters = self._starters, []
        for starter in pending:
            starter()


_CURRENT = contextvars.ContextVar("sonder_http_deferred_starts", default=None)


def current() -> DeferredStarts | None:
    return _CURRENT.get()


@contextmanager
def response_scope():
    collector = DeferredStarts()
    token = _CURRENT.set(collector)
    try:
        yield collector
    finally:
        _CURRENT.reset(token)


def defer(starter) -> bool:
    collector = current()
    if collector is None:
        return False
    collector.add(starter)
    return True


__all__ = [
    "APPROVAL_PARK_SECONDS", "DeferredStarts", "approval_hook", "admission_scope",
    "WORK_RUN_OUTCOMES", "classify_output", "classify_work_output", "current", "defer", "delegated_child", "enrich_work_record", "prepare_ack",
    "response_scope", "work_scope", "narration_scope",
    "admission_hook", "deferred_post",
    "run_scoped", "work_ack", "run_ack", "start_work", "admission_result",
]
