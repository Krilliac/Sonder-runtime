"""Adaptive lane scheduler, ownership claims, and stall-aware dispatcher."""
from __future__ import annotations

import contextlib
import contextvars
import logging
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from typing import Any

import sonder_runtime.domain.adaptive_concurrency as adaptive_concurrency
from sonder_runtime.application.agents import delegation_runtime as _delegation_rt


logger = logging.getLogger(__name__)

_MAX_LOGGED_ERROR_CHARS = 200


class FleetStalledError(RuntimeError):
    """A lane exceeded its progress deadline and remains quarantined alive."""

    def __init__(self, lanes: Iterable[object]) -> None:
        self.lanes: tuple[str, ...] = tuple(str(lane) for lane in lanes)
        super().__init__(
            "fleet lanes stalled without progress: %s" % ", ".join(self.lanes)
        )


def bounded_error_text(exc: BaseException) -> str:
    """One-line, length-bounded ``Type: message`` for operator logs."""
    text = " ".join(str(exc).split())
    if len(text) > _MAX_LOGGED_ERROR_CHARS:
        text = text[:_MAX_LOGGED_ERROR_CHARS] + "..."
    return "%s: %s" % (type(exc).__name__, text) if text else type(exc).__name__


def objective_assignments(
    objectives: Sequence[Any], count: int
) -> tuple[tuple[Any, ...], ...]:
    """Distribute authoritative objectives round-robin across ``count`` workers."""
    if not objectives:
        return ()
    buckets: list[list[Any]] = [[] for _ in range(count)]
    for index, objective in enumerate(objectives):
        buckets[index % count].append(objective)
    for index, bucket in enumerate(buckets):
        if not bucket:
            bucket.append(objectives[index % len(objectives)])
    return tuple(tuple(bucket) for bucket in buckets)


def delegated_lane_claims(
    child_ids: Sequence[str],
    assignments: Sequence[Sequence[Any]],
    project_root: str = "",
) -> list[adaptive_concurrency.LaneClaim]:
    """Build anchored READ ownership claims for delegated fleet lanes."""
    claims: list[adaptive_concurrency.LaneClaim] = []
    padded: Sequence[Sequence[Any]] = assignments or [() for _ in child_ids]
    for agent_id, assigned in zip(child_ids, padded, strict=False):
        paths = frozenset(
            adaptive_concurrency.anchor_claim_path(str(objective.path), project_root)
            for objective in assigned
        )
        claims.append(
            adaptive_concurrency.LaneClaim(
                agent_id, paths, adaptive_concurrency.LaneAccess.READ,
            )
        )
    return claims


def _default_resources() -> adaptive_concurrency.ResourceSnapshot:
    return adaptive_concurrency.ResourceSnapshot()


class AdaptiveLaneScheduler:
    """Dispatcher-side owner of the pure adaptive-concurrency policy.

    ``admit``/``complete``/``summary`` are called only by the dispatching
    thread.  Worker threads report outcomes through :meth:`sink`, which only
    appends to a locked inbox; the inbox is folded into the policy state on
    the dispatcher thread at the next completion.
    """

    def __init__(
        self,
        claims: Iterable[adaptive_concurrency.LaneClaim],
        ceiling: int,
        *,
        adaptive: bool = True,
        resources: Callable[[], adaptive_concurrency.ResourceSnapshot] | None = None,
        policy: adaptive_concurrency.ConcurrencyPolicy | None = None,
        on_decision: Callable[[dict[str, Any]], Any] | None = None,
    ) -> None:
        bounded_ceiling = max(1, int(ceiling))
        self.policy = policy or adaptive_concurrency.ConcurrencyPolicy(
            ceiling=bounded_ceiling
        )
        self.state = adaptive_concurrency.initial_state(self.policy)
        self.adaptive = bool(adaptive)
        claim_list = list(claims)
        self.graph = adaptive_concurrency.conflict_graph(claim_list)
        self._pending: list[str] = [claim.lane_id for claim in claim_list]
        self._running: set[str] = set()
        self._resources: Callable[[], adaptive_concurrency.ResourceSnapshot] = (
            resources if resources is not None else _default_resources
        )
        self._on_decision = on_decision
        self._inbox: list[tuple[str, adaptive_concurrency.Outcome]] = []
        self._inbox_lock = threading.Lock()
        self.decisions: list[dict[str, Any]] = []
        self.peak_running = 0
        self.serialized_waits = 0
        self.handler_failures = 0

    @property
    def cap(self) -> int:
        return int(self.state.cap)

    @property
    def pending(self) -> tuple[str, ...]:
        return tuple(self._pending)

    @property
    def running(self) -> frozenset[str]:
        return frozenset(self._running)

    def record_handler_failure(self) -> None:
        self.handler_failures += 1

    def sink(self, lane_id: str) -> Callable[[adaptive_concurrency.Outcome], None]:
        def record(outcome: adaptive_concurrency.Outcome) -> None:
            with self._inbox_lock:
                self._inbox.append((lane_id, outcome))

        return record

    def admit(self) -> tuple[str, ...]:
        admitted = adaptive_concurrency.admissible_lanes(
            self._pending, self._running, self.cap, self.graph,
        )
        if len(admitted) < len(self._pending) and (
            len(self._running) + len(admitted) < self.cap
        ):
            self.serialized_waits += 1
        for lane_id in admitted:
            self._pending.remove(lane_id)
            self._running.add(lane_id)
        self.peak_running = max(self.peak_running, len(self._running))
        return admitted

    def complete(self, lane_id: str) -> None:
        self._running.discard(lane_id)
        with self._inbox_lock:
            inbox, self._inbox = self._inbox, []
        if not self.adaptive:
            return
        if not inbox:
            inbox = [(lane_id, adaptive_concurrency.Outcome.SAMPLE)]
        try:
            resources = self._resources()
        except Exception:
            resources = adaptive_concurrency.ResourceSnapshot()
        for index, (source, outcome) in enumerate(inbox):
            self.state, decision = adaptive_concurrency.observe(
                self.state,
                outcome,
                self.policy,
                resources if index == len(inbox) - 1 else None,
                source=source,
            )
            if decision.action != "hold" or decision.reasons:
                record = decision.to_dict()
                record["lane"] = source
                self.decisions.append(record)
                if self._on_decision is not None:
                    with contextlib.suppress(Exception):
                        self._on_decision(record)

    def summary(self) -> dict[str, Any]:
        return {
            "adaptive": self.adaptive,
            "ceiling": int(self.policy.ceiling),
            "final_cap": self.cap,
            "peak_running": int(self.peak_running),
            "coupled_lanes": adaptive_concurrency.coupled_lane_count(self.graph),
            "serialized_waits": int(self.serialized_waits),
            "handler_failures": int(self.handler_failures),
            "decisions": list(self.decisions),
        }


def dispatch_lanes(
    scheduler: AdaptiveLaneScheduler,
    max_workers: int,
    run_lane: Callable[[str, Callable[[adaptive_concurrency.Outcome], None]], Any],
    collect: Callable[[str, Any], Any],
    on_error: Callable[[str, BaseException], Any],
    on_abandon: Callable[[str], Any] | None = None,
    on_stall: Callable[[tuple[str, ...]], Any] | None = None,
    *,
    executor_cls: Callable[..., Any] = ThreadPoolExecutor,
    wait_fn: Callable[..., tuple[set[Future[Any]], set[Future[Any]]]] = wait,
    get_agent_fn: Callable[[str], Mapping[str, Any] | None],
    progress_deadline_fn: Callable[[], float],
    lane_deadline_fn: Callable[[Mapping[str, Any] | None, float], float],
    heartbeat_seconds: float = 5.0,
) -> None:
    """Run every scheduled lane, admitting only what ``scheduler`` allows."""
    unsubmitted: list[str] = []

    def close(lane_id: str, when: str) -> None:
        if on_abandon is None:
            return
        try:
            on_abandon(lane_id)
        except Exception as abandon_exc:
            logger.warning(
                "fleet lane %s could not be closed %s: %s",
                lane_id, when, bounded_error_text(abandon_exc),
            )

    pool = executor_cls(max_workers=max(1, int(max_workers)))
    futures: dict[Future[Any], str] = {}
    started_at: dict[Future[Any], float] = {}
    last_progress_ts: dict[Future[Any], float | None] = {}
    progress_deadline = float(progress_deadline_fn())
    lane_deadline: dict[Future[Any], float] = {}
    wait_slice = max(0.1, min(float(heartbeat_seconds), progress_deadline / 4.0))
    try:
        def _run_lane_at_child_depth(
            target_lane_id: str,
            target_sink: Callable[[adaptive_concurrency.Outcome], None],
        ) -> Any:
            with _delegation_rt.depth_scope(1):
                return run_lane(target_lane_id, target_sink)

        def submit_admitted() -> None:
            admitted = list(scheduler.admit())
            while admitted:
                lane_id = admitted[0]
                ctx = contextvars.copy_context()
                try:
                    future = pool.submit(
                        ctx.run,
                        _run_lane_at_child_depth,
                        lane_id,
                        scheduler.sink(lane_id),
                    )
                except BaseException:
                    unsubmitted.extend(admitted)
                    raise
                admitted.pop(0)
                futures[future] = lane_id
                started_at[future] = time.monotonic()
                last_progress_ts[future] = None

        submit_admitted()
        while futures:
            done, pending = wait_fn(
                tuple(futures), timeout=wait_slice, return_when=FIRST_COMPLETED,
            )
            now = time.monotonic()
            for future in pending:
                lane_id = futures[future]
                with contextlib.suppress(Exception):
                    row = get_agent_fn(lane_id)
                    updated_ts = float(row.get("updated_ts") or 0) if row else 0.0
                    previous_ts = last_progress_ts.get(future)
                    if updated_ts and (
                        previous_ts is None or updated_ts > previous_ts
                    ):
                        last_progress_ts[future] = updated_ts
                        started_at[future] = now
                    lane_deadline[future] = lane_deadline_fn(row, progress_deadline)
            stalled = [
                future
                for future in pending
                if now - started_at[future]
                >= lane_deadline.get(future, progress_deadline)
            ]
            if stalled:
                stalled_lanes = [futures[future] for future in stalled]
                logger.warning(
                    "fleet lanes stalled; quarantining live workers: %s",
                    ", ".join(stalled_lanes),
                )
                if on_stall is not None:
                    on_stall(tuple(stalled_lanes))
                raise FleetStalledError(stalled_lanes)
            if not done:
                logger.info(
                    "fleet dispatch waiting on %d lane(s); progress deadline %.1fs",
                    len(futures), progress_deadline,
                )
                continue
            for future in done:
                lane_id = futures.pop(future)
                started_at.pop(future, None)
                last_progress_ts.pop(future, None)
                lane_deadline.pop(future, None)
                is_error = False
                try:
                    result = future.result()
                except Exception as exc:
                    scheduler.sink(lane_id)(adaptive_concurrency.Outcome.FAILED)
                    is_error = True
                    error_exc: BaseException = exc
                try:
                    if is_error:
                        on_error(lane_id, error_exc)
                    else:
                        collect(lane_id, result)
                except Exception as handler_exc:
                    scheduler.record_handler_failure()
                    logger.warning(
                        "fleet lane %s result handling failed: %s",
                        lane_id, bounded_error_text(handler_exc),
                    )
                    if is_error:
                        close(lane_id, "after its error handler failed")
                scheduler.complete(lane_id)
            submit_admitted()
    finally:
        for lane_id in unsubmitted + list(scheduler.pending):
            close(lane_id, "after dispatch abort")
        pool.shutdown(wait=False, cancel_futures=True)
