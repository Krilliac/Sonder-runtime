"""Agent-outcome helpers moved out of the size-ratcheted legacy modules.

Pure functions plus one process-local registry; no thread affinity beyond the
registry lock. Nothing here starts work or touches a model.
"""
from __future__ import annotations

import math
import random
import threading
import time
from collections.abc import Callable, Collection, Iterable, Mapping
from typing import Any, Protocol

from sonder_runtime.adapters.model_transport import ModelCallError

ROUTER_CAPACITY_KINDS = frozenset((
    "transport", "timeout", "busy_timeout", "provider_unavailable",
    "rate", "admission",  # host rate limit and GPU admission: retry later
))
ROUTER_DEFAULT_RETRY_SECONDS = 5
ROUTER_MAX_RETRY_SECONDS = 120


class StatusTracker(Protocol):
    """The activity tracker surface these helpers use (module or object)."""

    def set_response_status(self, status: str, summary: str = "") -> Any: ...

    def current(self) -> Mapping[str, Any] | None: ...


class DeadlineStore(Protocol):
    """A fleet store that reports the lane progress deadline."""

    def progress_deadline_seconds(self) -> float: ...


def router_capacity_error(error: object) -> bool:
    """True when a ModelCallError means 'no model capacity right now'."""
    if getattr(error, "kind", "") in ROUTER_CAPACITY_KINDS:
        return True
    if getattr(error, "retry_after_seconds", None):
        return True
    return bool(getattr(error, "transient", False))


def combined_cancel(
    cancel_check: Callable[[], object] | None, node: Any = None,
) -> Callable[[], object] | None:
    """A cancel probe that also fires for a foreground turn's cancel scope.

    ``node`` is the turn's CancellationNode, captured on the turn's own thread
    because the scope lives in a ContextVar that a model-call worker thread
    does not inherit. Cheap and thread-safe: it only reads flags.
    """
    if node is None:
        return cancel_check

    def probe() -> bool:
        return bool(node.cancelled) or (cancel_check is not None and bool(cancel_check()))

    return probe


def router_capacity_busy_error(error: object) -> ModelCallError:
    """The retryable 'capacity busy' error for a router that got no slot.

    It is an ``admission`` error, so the shared model-error formatter renders
    it with its existing ``Retry after about Ns.`` wording.
    """
    hint = getattr(error, "retry_after_seconds", None)
    seconds = (
        max(1, min(ROUTER_MAX_RETRY_SECONDS, math.ceil(hint)))
        if isinstance(hint, (int, float)) and math.isfinite(hint) and hint > 0
        else ROUTER_DEFAULT_RETRY_SECONDS
    )
    return ModelCallError(
        "admission",
        "model capacity busy; the execution-mode router could not get a "
        "model slot, so no work was started",
        transient=True, retry_after_seconds=seconds,
    )


class RouterCapacityBusy(str):
    """Answer text for a router that got no model slot, carrying typed facts.

    A ``str`` so every text consumer is unaffected; the work-run layer reads
    ``code``, ``status``, ``retryable`` and ``retry_after_seconds`` instead of
    parsing the sentence. Immutable value; any thread.
    """

    code = "MODEL_CAPACITY_BUSY"
    status = 429
    retryable = True
    retry_after_seconds: int

    def __new__(cls, text: str, *, retry_after_seconds: int) -> "RouterCapacityBusy":
        obj = super().__new__(cls, text)
        obj.retry_after_seconds = int(retry_after_seconds)
        return obj


def router_capacity_busy(
    error: object, render: Callable[[ModelCallError], str],
) -> RouterCapacityBusy:
    """The typed busy answer: the admission error rendered by ``render``."""
    busy = router_capacity_busy_error(error)
    return RouterCapacityBusy(
        render(busy), retry_after_seconds=int(busy.retry_after_seconds or ROUTER_DEFAULT_RETRY_SECONDS),
    )


def mark_failure_incomplete(
    tracker: StatusTracker, text: object, failed: bool = True,
) -> None:
    """Show a failed agent exit as ``incomplete`` in the activity/end report.

    The end report defaults to ``result: complete`` unless the host set a
    status, so an ERROR exit (repeat guard, parse failure, max_steps) read as
    a finished run. ``incomplete`` is the existing failed-run value.
    """
    if not failed:
        return
    first = next((line.strip() for line in str(text or "").splitlines() if line.strip()), "")
    current = tracker.current()
    if current is not None and current.get("status") == "incomplete":
        return
    tracker.set_response_status("incomplete", first or "agent run failed")


def unverified_banner(
    final: str, standing: str, prefix: str, phrase: str, tracker: StatusTracker,
) -> str:
    """The UNVERIFIED banner for a run that changed or delegated work.

    The activity/end-report status must agree with the host fence; otherwise
    users see ``result: complete`` directly above the warning and the model's
    claim wins the most visible field.
    """
    tracker.set_response_status("unverified", "agent completion lacks required verification")
    return "%s this run claimed completion without %s - %s\n\n%s" % (prefix, phrase, standing, final)


def model_request_failed(
    tracker: StatusTracker, store: dict[str, Any], error: object,
) -> None:
    """A failed model request: status ``incomplete`` (not ``complete``) plus its kind."""
    tracker.set_response_status("incomplete", "model request failed")
    note_model_failure(store, error)


def note_model_failure(store: dict[str, Any], error: object) -> None:
    """Record the typed kind a fleet worker's caller needs, as receipt fields."""
    store["model_failure_kind"] = str(getattr(error, "kind", "") or "unknown")
    store["model_failure_transient"] = bool(getattr(error, "transient", False))
    store["model_failure_pool_refusal"] = str(getattr(error, "pool_refusal_kind", "") or "")
    store["model_failure_admission_remaining"] = _window_seconds(
        getattr(error, "admission_remaining_seconds", None),
    )


def _window_seconds(value: object) -> float | None:
    """A finite, non-negative admission-window remainder, else None (unknown)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return max(0.0, float(value)) if math.isfinite(value) else None


class WorkerModelFailure(RuntimeError):
    """A worker run that ended on a typed model failure (never on evidence).

    ``failure_class`` is already in the fanout vocabulary, so the worker
    classifier returns it directly instead of guessing from message text.
    """

    def __init__(
        self, kind: str, transient: bool, detail: str,
        pool_refusal: str = "", admission_remaining: float | None = None,
    ) -> None:
        self.kind = str(kind or "unknown")
        self.transient = bool(transient)
        self.pool_refusal = str(pool_refusal or "")
        self.admission_remaining = _window_seconds(admission_remaining)
        if self.kind == "timeout":
            self.failure_class = "timeout"
        elif self.transient or self.kind in {
            "transport", "busy_timeout", "provider_unavailable",
        }:
            self.failure_class = "transport"
        else:
            self.failure_class = "unknown"
        super().__init__(
            "repository worker model call failed (%s): %s"
            % (self.kind, detail or "no detail")
        )


def leading_error_line(output: object) -> str:
    """The first line of a worker's output when it reports an ERROR."""
    for line in str(output or "").splitlines():
        line = line.strip()
        if line:
            return line[:400] if line.startswith("ERROR") else ""
    return ""


def no_evidence_error(receipt: object) -> RuntimeError:
    """Why a scoped receipt holds no file evidence: model failure or not.

    Only reached once the scope check passed and no evidence tool was
    observed, so a receipt that did collect evidence is never discarded here.
    """
    leading = leading_error_line(getattr(receipt, "output", ""))
    kind = str(getattr(receipt, "model_failure_kind", "") or "")
    if kind:
        return WorkerModelFailure(
            kind, bool(getattr(receipt, "model_failure_transient", False)), leading,
            pool_refusal=str(getattr(receipt, "model_failure_pool_refusal", "") or ""),
            admission_remaining=getattr(receipt, "model_failure_admission_remaining", None),
        )
    return RuntimeError(
        "repository worker produced no host-observed file evidence"
        + ("; worker reported: %s" % leading if leading else "")
    )


_RETRY_BACKOFF_BASE_SECONDS = (2.0, 4.0, 8.0)
_RETRY_JITTER = 0.25


def retry_backoff_seconds(attempt: int, remaining: float) -> float:
    """Jittered 2, 4, 8 s wait before retry number ``attempt``, capped.

    ``remaining`` is what is left of the lane's progress deadline; a retry
    never waits past it, and a spent deadline yields no wait at all.
    """
    index = min(max(int(attempt), 1), len(_RETRY_BACKOFF_BASE_SECONDS)) - 1
    base = _RETRY_BACKOFF_BASE_SECONDS[index]
    delay = base * random.uniform(1.0 - _RETRY_JITTER, 1.0 + _RETRY_JITTER)
    return max(0.0, min(delay, float(remaining)))


def retry_remaining_seconds(store: DeadlineStore) -> float:
    """Seconds a retry may still use: one lane progress deadline from now.

    Measured at the retry event, not at worker start, so a long first attempt
    does not turn the cap into a zero-second replay.
    """
    return float(getattr(store, "progress_deadline_seconds", lambda: 120.0)())


def _pool_refusal(exc: object) -> tuple[str, float | None]:
    """Typed pool refusal kind and admission-window remainder from an error.

    Reads attributes only, never message text. A bare WorkerPoolBackpressure
    is a backpressure refusal with an unknown window.
    """
    kind = str(
        getattr(exc, "pool_refusal", "") or getattr(exc, "pool_refusal_kind", "") or ""
    )
    window = getattr(exc, "admission_remaining", None)
    if window is None:
        window = getattr(exc, "admission_remaining_seconds", None)
    if not kind:
        from sonder_runtime.adapters.inference import ollama_pool
        if isinstance(exc, ollama_pool.WorkerPoolBackpressure):
            kind = "backpressure"
    return kind, _window_seconds(window)


def retry_wait_seconds(exc: object, attempt: int, remaining: float) -> float | None:
    """Seconds to wait before retry ``attempt``, or None when it must not run.

    No retry when nothing remains of the cap. A backpressure refusal that
    arrived inside its admission window, or whose window is unknown (fails
    closed), is never retried. Everything else gets the jittered backoff.
    """
    if not remaining > 0:
        return None
    kind, window = _pool_refusal(exc)
    if kind == "backpressure" and (window is None or window > 0):
        return None
    return retry_backoff_seconds(attempt, remaining)


def retry_plan(
    exc: object, attempt: int, attempts_allowed: int, failure_class: str,
    transient_classes: Collection[str], store: DeadlineStore,
    cancelled: Callable[[], object],
) -> float | None:
    """Backoff seconds when a worker failure may be retried, else None."""
    if attempt >= attempts_allowed or failure_class not in transient_classes:
        return None
    wait = retry_wait_seconds(exc, attempt, retry_remaining_seconds(store))
    return None if wait is None or cancelled() else wait


def retry_sleep(seconds: float, cancelled: Callable[[], object]) -> None:
    """Wait ``seconds`` in short slices so a cancel ends the wait promptly."""
    end = time.monotonic() + max(0.0, float(seconds))
    while True:
        remaining = end - time.monotonic()
        if remaining <= 0 or cancelled():
            return
        time.sleep(min(0.25, remaining))


def retry_pause(seconds: float, cancelled: Callable[[], object]) -> bool:
    """Back off, then report whether a cancel arrived during the wait."""
    retry_sleep(seconds, cancelled)
    return bool(cancelled())


# Lanes whose final failure was transient, so the master can say "model
# capacity unavailable" instead of blaming repository evidence. Keyed by the
# unique child agent id and consumed (popped) by the master that owns it.
_TRANSIENT_LANE_FAILURES: dict[str, str] = {}
_TRANSIENT_LANE_FAILURES_LOCK = threading.Lock()
_TRANSIENT_LANE_FAILURES_MAX = 2048


def remember_transient_failure(
    agent_id: str, failure_class: str, transient_classes: Collection[str],
) -> None:
    if failure_class not in transient_classes:
        return
    with _TRANSIENT_LANE_FAILURES_LOCK:
        _TRANSIENT_LANE_FAILURES[agent_id] = failure_class
        while len(_TRANSIENT_LANE_FAILURES) > _TRANSIENT_LANE_FAILURES_MAX:
            _TRANSIENT_LANE_FAILURES.pop(next(iter(_TRANSIENT_LANE_FAILURES)))


def take_transient_failures(agent_ids: Iterable[str]) -> dict[str, str]:
    with _TRANSIENT_LANE_FAILURES_LOCK:
        return {
            agent_id: failure
            for agent_id in agent_ids
            if (failure := _TRANSIENT_LANE_FAILURES.pop(agent_id, None))
        }


def capacity_unavailable_error(repository_task: object, child_ids: Collection[str]) -> str:
    """The master's error when every lane failed on pool/model capacity.

    Always consumes the recorded lane failures. Empty for a non-repository
    task, or when at least one lane failed for another reason (or none ran),
    so the caller keeps its evidence-required answer for those.
    """
    transient_lanes = take_transient_failures(child_ids)
    if not repository_task or not child_ids or len(transient_lanes) != len(child_ids):
        return ""
    return (
        "model capacity unavailable; retry (all delegated workers failed "
        "before producing an auditable result on a transient model or pool "
        "error: %s)" % ", ".join(sorted(set(transient_lanes.values())))
    )
