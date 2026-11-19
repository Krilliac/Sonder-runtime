"""Bounded execution of routed HTTP work: wall budget, cancel, persisted answer.

A chat turn that the host routes to a workbench, fleet, or autopilot lane used
to run synchronously inside the HTTP request with only a *step* bound.  A slow
model turned that into tens of minutes; a client that timed out lost the
answer, and nothing could stop the run.  This module gives each routed turn:

* a **run id** recorded durably before the lane starts (``http_work_runs``);
* a **wall-clock budget**.  Past it, and after an explicit **cancel**, the
  run's effect fence stops holding, so ``permission_modes.decide`` refuses
  every further file change, host program, or destructive tool the lane
  attempts (``source="fence"``).  The lane's body also runs inside a
  ``foreground_turns.check_scope`` on that fence, so the model-request and
  step boundaries the lane already consults (``cancel_requested()``) stop it
  at its next checkpoint instead of running on to its step bound.  A model
  call already in flight is not preempted from the HTTP layer; its answer is
  discarded by the lane;
* a **bounded wait**: the request waits at most ``wait_seconds`` and then
  answers with the run id.  The lane keeps its admission slot here (bounded by
  ``max_running``), and its answer is persisted for ``GET /v1/work-runs/<id>``;
* **approval parking**: a lane that stops on an operator approval parks (see
  :class:`RunHandle`).  A parked run holds no ``max_running`` slot and takes
  one again when it resumes.  Only the run's own thread releases the slot; a
  thread that inherited the run's context waits without touching slot state.

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
from typing import Callable, ContextManager

from sonder_runtime.application import foreground_turns


_LOG = logging.getLogger(__name__)

DEFAULT_WAIT_SECONDS = 240
DEFAULT_BUDGET_SECONDS = 1800
DEFAULT_MAX_RUNNING = 2
# 0 means "derive it": max(AUTO_MIN_LIVE, max_running), so a config that only
# raises max_running keeps working without also raising the live bound.
DEFAULT_MAX_LIVE = 0
AUTO_MIN_LIVE = 8
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
    parked: bool = False
    park_depth: int = 0
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


class RunHandle:
    """What a run's body scope may ask of its runner: abort state and parking.

    Thread affinity: created on the run's own thread, but reachable from any
    thread that inherited the run's context (delegated children, speculative
    read-only dispatch).  Only the run's own thread releases the routed-work
    slot; other threads wait without touching slot state.  A speculative
    read-only dispatch thread may still run a tool (no model call) while the
    owner thread is parked.
    """

    def __init__(self, runner: "WorkRunner", run: _Run, fence):
        self._runner = runner
        self._run = run
        self._fence = fence
        self.run_id = run.run_id
        self._owner = threading.get_ident()

    def on_run_thread(self) -> bool:
        """True when called on the thread that runs the work body."""
        return threading.get_ident() == self._owner

    def aborted(self) -> bool:
        """True once the run was cancelled, ran out of budget, or the host is draining."""
        return bool(self._runner._effects.reason_lost(self._fence))

    @contextlib.contextmanager
    def parked(self):
        """Release this run's routed-work slot while it waits on an operator.

        Yields ``True`` when the slot was released (the run's own thread) and
        ``False`` on any other thread, which waits without touching slot state.
        On the run's own thread, parking nests (depth-counted); on the outermost
        exit the run re-admits: it waits for a free slot, and gives up the wait
        only once the run is aborted (so a cancel or the wall budget always
        ends a wait).
        """
        if not self.on_run_thread():
            yield False
            return
        runner, run = self._runner, self._run
        with runner._lock:
            run.park_depth += 1
            run.parked = True
        try:
            yield True
        finally:
            with runner._lock:
                run.park_depth -= 1
                outermost = run.park_depth <= 0
            while outermost:
                with runner._lock:
                    busy = sum(1 for other in runner._runs.values()
                               if other is not run and not other.parked)
                    if busy < runner.max_running:
                        run.parked = False
                        break
                if self.aborted():
                    with runner._lock:
                        run.parked = False
                    break
                threading.Event().wait(0.05)


class WorkRunner:
    """Process-wide registry of routed HTTP work runs."""

    def __init__(self, *, store, effects, wait_seconds=DEFAULT_WAIT_SECONDS,
                 budget_seconds=DEFAULT_BUDGET_SECONDS,
                 max_running=DEFAULT_MAX_RUNNING, max_live=DEFAULT_MAX_LIVE,
                 clock=time.monotonic,
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
                       max_running=max_running, max_live=max_live)
        self._clock = clock
        self._lock = threading.Lock()
        self._runs: dict[str, _Run] = {}
        self._reconciled = False

    def configure(self, *, wait_seconds, budget_seconds, max_running,
                  max_live=DEFAULT_MAX_LIVE):
        budget = max(30, min(24 * 3600, int(budget_seconds)))
        self.budget_seconds = budget
        self.wait_seconds = max(1, min(budget, int(wait_seconds)))
        self.max_running = max(1, min(64, int(max_running)))
        # Total live runs, parked ones included: a parked run frees its running
        # slot but still costs a thread and a poll loop, so it needs its own bound.
        live = int(max_live)
        self.max_live = (max(AUTO_MIN_LIVE, self.max_running) if live <= 0
                         else max(self.max_running, min(256, live)))

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
        """Runs in flight, parked ones included (a drain must wait for them)."""
        with self._lock:
            return len(self._runs)

    def slots_in_use(self) -> int:
        """Runs holding a routed-work slot: in flight and not parked on an approval."""
        with self._lock:
            return sum(1 for run in self._runs.values() if not run.parked)

    def run(self, principal: str, call: Callable[[], object], *,
            classify: Callable[[object], tuple[str, str]],
            thread_wrapper: Callable[[Callable[[], None]], Callable[[], None]] | None = None,
            wait_seconds: int | float | None = None,
            on_admitted: Callable[[str], None] | None = None,
            defer_start: Callable[[Callable[[], None]], bool] | None = None,
            body_scope: Callable[[RunHandle], ContextManager] | None = None,
            ) -> WorkOutcome:
        """Start ``call`` under a fence and wait up to the wait budget.

        ``classify(result)`` returns ``(status, answer_text)`` for the durable
        record.  Exceptions raised while the caller is still waiting are
        re-raised to it unchanged; later ones are recorded as ``failed``.

        ``body_scope(handle)`` returns a context manager entered on the run's
        own thread around ``call()``: the composition root uses it to re-enter
        the admission-time permission scope and install the approval hook.
        """
        if not self._reconciled:
            try:
                self.reconcile()
            except Exception:
                _LOG.error("HTTP work run reconciliation failed", exc_info=True)
        run_id = "wr-" + uuid.uuid4().hex
        run = _Run(run_id, self._clock() + self.budget_seconds)
        with self._lock:
            if len(self._runs) >= self.max_live:
                raise WorkCapacityExhausted(
                    "%d routed work run(s) are already live (running or awaiting approval)"
                    % len(self._runs)
                )
            held = sum(1 for other in self._runs.values() if not other.parked)
            if held >= self.max_running:
                raise WorkCapacityExhausted(
                    "%d routed work run(s) are already running" % held
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
        if on_admitted is not None:
            try:
                on_admitted(run_id)
            except BaseException:
                try:
                    self._store.finish(run_id, "failed", "Acknowledgement could not be recorded; work did not start.")
                finally:
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
                scope = (body_scope(RunHandle(self, run, fence)) if body_scope is not None
                         else contextlib.nullcontext())
                with (
                    lifetime,
                    self._effects.held(fence),
                    scope,
                    foreground_turns.check_scope(lambda: bool(self._effects.reason_lost(fence))),
                ):
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

        def start_worker():
            try:
                worker = self._thread_factory(
                    target=context.run, args=(target,), name="sonder-http-work-" + run_id[-8:],
                    daemon=True,
                )
                worker.start()
            except BaseException:
                # body() never ran, so its cleanup did not either: free the slot
                # and terminalize the durable row instead of leaving it running.
                try:
                    self._store.finish(run_id, "failed", "")
                except Exception:
                    _LOG.error("HTTP work run %s could not be marked failed", run_id, exc_info=True)
                with self._lock:
                    self._runs.pop(run_id, None)
                run.done.set()
                raise

        try:
            deferred = bool(defer_start(start_worker)) if defer_start is not None else False
        except BaseException:
            try:
                self._store.finish(run_id, "failed", "Work could not be queued.")
            finally:
                with self._lock:
                    self._runs.pop(run_id, None)
            raise
        if not deferred:
            start_worker()
        wait_for = self.wait_seconds if wait_seconds is None else max(0.0, min(
            float(self.budget_seconds), float(wait_seconds),
        ))
        # A zero wait is the explicit fire-and-status mode used by narrated
        # HTTP admissions.  Always return the receipt, even if a tiny worker
        # completed during thread startup; its terminal record is available
        # through the status route.
        if wait_for == 0:
            waiter["attached"] = False
            return WorkOutcome(run_id, False)
        if not run.done.wait(wait_for):
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
    "DEFAULT_BUDGET_SECONDS", "AUTO_MIN_LIVE", "DEFAULT_MAX_LIVE", "DEFAULT_MAX_RUNNING", "DEFAULT_WAIT_SECONDS",
    "PROCESS_ID", "RunHandle", "WorkCapacityExhausted", "current_run_id", "WorkOutcome", "WorkRunner",
    "owner_scope",
]
