"""Bounded execution of routed HTTP work: wall budget, cancel, persisted answer.

A chat turn that the host routes to a workbench, fleet, or autopilot lane used
to run synchronously inside the HTTP request with only a *step* bound.  A slow
model turned that into tens of minutes; a client that timed out lost the
answer, and nothing could stop the run.  This module gives each routed turn:

* a **run id** recorded durably before the lane starts (``http_work_runs``);
* a **wall-clock budget**.  Past it, and after an explicit **cancel**, the
  run's effect fence stops holding, so ``permission_modes.decide`` refuses
  every further file change, host program, or destructive tool the lane
  attempts (``source="fence"``).  The lane's remaining model steps still run
  to their step bound -- a model call already in flight cannot be preempted
  from the HTTP layer -- but they can no longer change anything;
* a **bounded wait**: the request waits at most ``wait_seconds`` and then
  answers with the run id.  The lane keeps its admission slot here (bounded by
  ``max_running``), and its answer is persisted for ``GET /v1/work-runs/<id>``.

Runs are process-local threads; a restart reconciles rows left ``running`` to
``interrupted``.

The durable store (``adapters.persistence.http_work_runs``) and the effect
fence (``adapters.execution.effect_fence``) are injected by the HTTP
composition in ``serve.py``; this module stays free of adapter imports.
"""
from __future__ import annotations

import contextlib
import contextvars
import hashlib
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable


_LOG = logging.getLogger(__name__)

DEFAULT_WAIT_SECONDS = 240
DEFAULT_BUDGET_SECONDS = 1800
DEFAULT_MAX_RUNNING = 2
# A fixed identity for this process: rows written by another process id and
# still ``running`` can only be leftovers of a process that stopped.
PROCESS_ID = "proc-" + uuid.uuid4().hex


_CURRENT_RUN: contextvars.ContextVar[str] = contextvars.ContextVar(
    "sonder_http_work_run", default="",
)


def current_run_id() -> str:
    """The work run executing on this thread, or ``""``."""
    return _CURRENT_RUN.get()


class WorkCapacityExhausted(RuntimeError):
    """Every routed-work slot is taken; the new run was not started."""


@dataclass
class _Run:
    run_id: str
    deadline: float
    done: threading.Event = field(default_factory=threading.Event)
    result: object = None
    error: BaseException | None = None


@dataclass(frozen=True)
class WorkOutcome:
    """``finished`` carries the lane's result; otherwise it is still running."""

    run_id: str
    finished: bool
    result: object = None


def owner_scope(principal: str) -> str:
    """Opaque per-principal key; the store never holds account identity."""
    material = "http-work-run-owner\0" + str(principal or "")
    return "wo-" + hashlib.sha256(material.encode("utf-8")).hexdigest()


class WorkRunner:
    """Process-wide registry of routed HTTP work runs."""

    def __init__(self, *, store, effects, wait_seconds=DEFAULT_WAIT_SECONDS,
                 budget_seconds=DEFAULT_BUDGET_SECONDS,
                 max_running=DEFAULT_MAX_RUNNING, clock=time.monotonic,
                 stop_reason: Callable[[], str] | None = None,
                 lifetime: Callable[[], object] | None = None,
                 thread_factory: Callable[..., object]):
        # ``store``: start/finish/get/recent/request_cancel/cancel_requested/
        # reconcile.  ``effects``: Fence, held(fence), reason_lost(fence).
        # ``stop_reason()``: a process-wide reason every run must stop
        # changing things (the runtime is draining), or "".  ``lifetime()``:
        # a context manager held by the run's thread for its whole life, so a
        # graceful drain can count a run that outlived its request.
        self._store = store
        self._effects = effects
        self._stop_reason = stop_reason
        self._lifetime = lifetime
        # The runtime's owned-thread constructor, injected by the serving entry
        # point: interfaces may not import the platform layer directly.
        self._thread_factory = thread_factory
        self.configure(wait_seconds=wait_seconds, budget_seconds=budget_seconds,
                       max_running=max_running)
        self._clock = clock
        self._lock = threading.Lock()
        self._runs: dict[str, _Run] = {}
        self._reconciled = False

    def configure(self, *, wait_seconds, budget_seconds, max_running):
        budget = max(30, min(24 * 3600, int(budget_seconds)))
        self.budget_seconds = budget
        self.wait_seconds = max(1, min(budget, int(wait_seconds)))
        self.max_running = max(1, min(64, int(max_running)))

    # -- lifecycle ---------------------------------------------------------

    def reconcile(self) -> int:
        """Mark runs an earlier process left ``running`` as ``interrupted``."""
        changed = self._store.reconcile(PROCESS_ID)
        self._reconciled = True
        if changed:
            _LOG.warning("reconciled %d interrupted HTTP work run(s) from an earlier process", changed)
        return changed

    def _fence(self, run: _Run):
        run_id = run.run_id

        def check() -> str:
            if self._stop_reason is not None:
                stopping = self._stop_reason()
                if stopping:
                    return "HTTP work run %s was interrupted: %s" % (run_id, stopping)
            if self._clock() >= run.deadline:
                return "HTTP work run %s exceeded its wall-clock budget" % run_id
            if self._store.cancel_requested(run_id):
                return "HTTP work run %s was cancelled" % run_id
            return ""

        return self._effects.Fence("http-work:%s" % run_id, check)

    def running_count(self) -> int:
        with self._lock:
            return len(self._runs)

    def run(self, principal: str, call: Callable[[], object], *,
            classify: Callable[[object], tuple[str, str]],
            thread_wrapper: Callable[[Callable[[], None]], Callable[[], None]] | None = None,
            ) -> WorkOutcome:
        """Start ``call`` under a fence and wait up to the wait budget.

        ``classify(result)`` returns ``(status, answer_text)`` for the durable
        record.  Exceptions raised while the caller is still waiting are
        re-raised to it unchanged; later ones are recorded as ``failed``.
        """
        if not self._reconciled:
            try:
                self.reconcile()
            except Exception:
                _LOG.error("HTTP work run reconciliation failed", exc_info=True)
        run_id = "wr-" + uuid.uuid4().hex
        run = _Run(run_id, self._clock() + self.budget_seconds)
        with self._lock:
            if len(self._runs) >= self.max_running:
                raise WorkCapacityExhausted(
                    "%d routed work run(s) are already running" % len(self._runs)
                )
            self._runs[run_id] = run
        try:
            self._store.start(
                run_id, owner_scope=owner_scope(principal), process_id=PROCESS_ID,
                deadline_ts=time.time() + self.budget_seconds,
            )
        except BaseException:
            with self._lock:
                self._runs.pop(run_id, None)
            raise
        fence = self._fence(run)
        waiter = {"attached": True}

        def body():
            status, text = "failed", ""
            token = _CURRENT_RUN.set(run_id)
            try:
                lifetime = (self._lifetime() if self._lifetime is not None
                            else contextlib.nullcontext())
                with lifetime, self._effects.held(fence):
                    run.result = call()
                status, text = classify(run.result)
            except BaseException as error:  # recorded, then surfaced if attached
                run.error = error
                _LOG.error("HTTP work run %s raised", run_id, exc_info=not waiter["attached"])
            finally:
                _CURRENT_RUN.reset(token)
                lost = self._effects.reason_lost(fence)
                if status not in ("refused",) and lost:
                    status = (
                        "interrupted" if "interrupted" in lost
                        else "cancelled" if "cancelled" in lost
                        else "budget_exceeded"
                    )
                try:
                    self._store.finish(run_id, status, text)
                except Exception:
                    _LOG.error("HTTP work run %s result could not be persisted", run_id, exc_info=True)
                with self._lock:
                    self._runs.pop(run_id, None)
                run.done.set()

        target = thread_wrapper(body) if thread_wrapper is not None else body
        context = contextvars.copy_context()
        worker = self._thread_factory(
            target=context.run, args=(target,), name="sonder-http-work-" + run_id[-8:],
            daemon=True,
        )
        worker.start()
        if not run.done.wait(self.wait_seconds):
            waiter["attached"] = False
            # Close the race with a run that finished just after the wait.
            if not run.done.is_set():
                return WorkOutcome(run_id, False)
        if run.error is not None:
            raise run.error
        return WorkOutcome(run_id, True, run.result)

    # -- caller surface ------------------------------------------------------

    def get(self, run_id: str, principal: str) -> dict | None:
        return self._store.get(run_id, owner_scope=owner_scope(principal))

    def recent(self, principal: str, limit: int = 20) -> list[dict]:
        return self._store.recent(owner_scope=owner_scope(principal), limit=limit)

    def cancel(self, run_id: str, principal: str) -> dict | None:
        return self._store.request_cancel(run_id, owner_scope=owner_scope(principal))

__all__ = [
    "DEFAULT_BUDGET_SECONDS", "DEFAULT_MAX_RUNNING", "DEFAULT_WAIT_SECONDS",
    "PROCESS_ID", "WorkCapacityExhausted", "current_run_id", "WorkOutcome", "WorkRunner",
    "owner_scope",
]
