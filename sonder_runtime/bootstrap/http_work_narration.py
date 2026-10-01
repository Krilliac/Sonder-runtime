"""Request-local deferred starts for narrated HTTP work admissions."""
from __future__ import annotations

import contextvars
import functools
from contextlib import contextmanager

from sonder_runtime.bootstrap.work_narration import (
    enrich_work_record, prepare_ack, work_scope,
)
from sonder_runtime.adapters.observability import activity_tracker
from sonder_runtime.application.chat.handoff_receipts import ChatWorkResult


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


def run_scoped(run_id, link_run, acknowledgement, activity_id, function):
    from sonder_runtime.application.ports.work_narration import bind_activity
    with narration_scope(run_id, link_run, acknowledgement, activity_id):
        with activity_tracker.response_span("work run", surface="http-work"):
            bind_activity(activity_tracker.current_response_id() or "")
            return function()


def deferred_post(function):
    def wrapped(self):
        with response_scope() as deferred:
            try:
                return function(self)
            finally:
                deferred.flush()
    wrapped.__name__ = getattr(function, "__name__", "do_POST")
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
    "DeferredStarts", "current", "defer", "enrich_work_record", "prepare_ack",
    "response_scope", "work_scope", "narration_scope",
    "admission_hook", "deferred_post",
    "run_scoped", "work_ack", "run_ack", "start_work", "admission_result",
]
