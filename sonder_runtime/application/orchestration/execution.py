"""Worker, inline, delegated, and background orchestration execution flows."""
from __future__ import annotations

import contextlib
import hashlib
import json
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, ContextManager

import sonder_runtime.domain.adaptive_concurrency as adaptive_concurrency
from sonder_runtime.application.artifacts import ArtifactReadiness
from sonder_runtime.application.artifacts.master_fanin import (
    validate_master_slots_for_run,
)
from sonder_runtime.application.orchestration.capacity import (
    ABORT_MARKERS,
    EVIDENCE_REQUIRED,
    TRANSIENT_FAILURE_CLASSES,
    positive_int,
    requires_repository_tools,
)
from sonder_runtime.application.orchestration.scheduler import (
    AdaptiveLaneScheduler,
    FleetStalledError,
    delegated_lane_claims,
    objective_assignments,
)


@dataclass(frozen=True)
class RepositoryWorkerResult:
    """Host-issued repository result with an exact filesystem scope receipt."""

    output: str
    project: str
    tools: tuple[str, ...]
    produced_files: tuple[str, ...] = ()
    checks_run: bool = False
    checks_passed: bool | None = None


@dataclass(frozen=True)
class ReadyWorkerOutput:
    """Producer-owned output plus its finalized readiness evidence."""

    output: object
    readiness: ArtifactReadiness


def readiness_verifier_receipt(
    agent_id: str,
    run_id: str,
    delegated_task_digest: str,
    output: str,
    verification: Mapping[str, Any],
    *,
    task_digest_fn: Callable[[str], str],
) -> str:
    """Digest the host's completion/provenance checks for an artifact join."""
    payload = {
        "run_id": run_id,
        "producer_id": agent_id,
        "source_task_digest": delegated_task_digest,
        "completed_output_digest": task_digest_fn(output),
        "verification": verification,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True)
class WorkerExecutionHooks:
    """Injectable host callbacks for :func:`execute_worker`."""

    start_agent: Callable[..., bool]
    begin_model_call: Callable[..., bool]
    finish: Callable[..., str]
    event: Callable[[str, str], None]
    bind_worker_agent: Callable[[str], ContextManager[Any]]
    cancel_requested: Callable[[str], bool]
    validate_delegation: Callable[..., dict[str, Any]]
    validate_result: Callable[..., dict[str, Any]]
    validate_repository_result: Callable[[Any, str], RepositoryWorkerResult]
    render_repository_result: Callable[[RepositoryWorkerResult], str]
    attach_creation_receipt: Callable[[RepositoryWorkerResult, str], RepositoryWorkerResult]
    classify_worker_error: Callable[[BaseException], str]
    retry_plan: Callable[..., float | None]
    retry_pause: Callable[[float, Callable[[], bool]], bool]
    remember_transient_failure: Callable[[str, str, frozenset[str]], None]
    worker_transient_retries: Callable[[], int]
    task_digest: Callable[[str], str]
    worker_failed_sentinel: object


@dataclass(frozen=True)
class InlineExecutionHooks:
    """Injectable host callbacks for :func:`execute_inline`."""

    parse_objectives: Callable[[str], Sequence[Any]]
    resolve_repository_project_root: Callable[[str, str], str]
    task_digest: Callable[[str], str]
    objective_contract: Callable[[Sequence[Any]], str]
    new_agent: Callable[..., str]
    start_agent: Callable[..., bool]
    begin_model_call: Callable[..., bool]
    finish: Callable[..., str]
    bind_worker_agent: Callable[[str], ContextManager[Any]]
    validate_delegation: Callable[..., dict[str, Any]]
    validate_result: Callable[..., dict[str, Any]]
    validate_repository_result: Callable[[Any, str], RepositoryWorkerResult]
    render_repository_result: Callable[[RepositoryWorkerResult], str]


@dataclass(frozen=True)
class DelegatedExecutionHooks:
    """Injectable host callbacks for :func:`execute_delegated`."""

    parse_objectives: Callable[[str], Sequence[Any]]
    resolve_repository_project_root: Callable[[str, str], str]
    same_project_root: Callable[[str, str], bool]
    explicit_agent_ceiling: Callable[[], int]
    clamp_agent_count: Callable[..., int]
    capacity: Callable[..., dict[str, Any]]
    parallel_worker_slots: Callable[..., int]
    task_digest: Callable[[str], str]
    objective_contract: Callable[[Sequence[Any]], str]
    subtask_prompts: Callable[..., list[str]]
    new_agent: Callable[..., str]
    start_agent: Callable[..., bool]
    begin_model_call: Callable[..., bool]
    finish: Callable[..., str]
    event: Callable[[str, str], None]
    request_cancel: Callable[[str], Any]
    cancel_requested: Callable[[str], bool]
    release_reservation: Callable[[str], None]
    bind_worker_agent: Callable[[str], ContextManager[Any]]
    run_worker: Callable[..., Any]
    dispatch_lanes: Callable[..., None]
    scheduler_cls: type[AdaptiveLaneScheduler]
    adaptive_concurrency_enabled: Callable[[], bool]
    render_repository_result: Callable[[RepositoryWorkerResult], str]
    public_outputs: Callable[[Sequence[tuple[str, Any]]], list[tuple[str, str]]]
    validate_result: Callable[..., dict[str, Any]]
    aggregation_metrics: Callable[..., dict[str, Any]]
    validate_aggregate_output: Callable[..., dict[str, Any]]
    capacity_unavailable_error: Callable[[bool, Sequence[str]], str]
    create_workspace: Callable[[str, int], Any]
    release_workspace: Callable[[Any], None]
    build_report: Callable[[Any, Mapping[str, str], Sequence[tuple[str, Any]]], str]
    audit_prompt: Callable[..., str]
    worker_failed_sentinel: object


def execute_worker(
    agent_id: str,
    prompt: str,
    worker_fn: Callable[..., Any],
    *,
    hooks: WorkerExecutionHooks,
    project_scope: str = "",
    master_task: str = "",
    objectives: Sequence[Any] = (),
    master_task_digest: str = "",
    delegated_task_digest: str = "",
    run_id: str = "",
    outcome_sink: Callable[[adaptive_concurrency.Outcome], None] | None = None,
    strategy_observer: Callable[..., bool] | None = None,
    model_route: str = "",
    creation_worker_root: str = "",
) -> Any:
    """Execute one delegated worker lane with provenance checks and bounded retry."""

    def report(outcome: adaptive_concurrency.Outcome) -> None:
        if outcome_sink is not None:
            with contextlib.suppress(Exception):
                outcome_sink(outcome)

    attempts_allowed = 1 + hooks.worker_transient_retries()

    def observe_strategy(
        attempt_number: int,
        *,
        accepted: bool,
        failure_code: str = "",
        legacy_retry: bool = False,
    ) -> bool:
        if strategy_observer is None:
            return legacy_retry
        return bool(
            strategy_observer(
                agent_id=agent_id,
                master_id=run_id,
                prompt=prompt,
                master_digest=master_task_digest,
                project_scope=project_scope,
                attempt_number=attempt_number,
                attempt_limit=attempts_allowed,
                route=model_route,
                accepted=accepted,
                failure_code=failure_code,
                legacy_retry=legacy_retry,
            )
        )

    pre_call_metrics: dict[str, Any] | None = None
    if objectives:
        if not hooks.start_agent(agent_id, "validating delegated task provenance"):
            return "CANCELLED"
        metrics = hooks.validate_delegation(
            master_task,
            prompt,
            objectives,
            expected_master_digest=master_task_digest,
            expected_delegated_digest=delegated_task_digest,
            project=project_scope,
        )
        if metrics["task_drift"]:
            hooks.finish(
                agent_id,
                error="delegated task provenance changed before model call",
                task_drift=True,
                drift_metrics=metrics,
            )
            report(adaptive_concurrency.Outcome.CHURN)
            return hooks.worker_failed_sentinel
        pre_call_metrics = metrics
        if not hooks.begin_model_call(
            agent_id, "calling model for provenance-validated task", tool_calls=1,
        ):
            final = hooks.finish(agent_id)
            return final or "CANCELLED"
    elif not hooks.start_agent(
        agent_id, "calling model for delegated task", tool_calls=1,
        in_model_call=True,
    ):
        return "CANCELLED"

    # Re-read transient retries after agent start to match historical order.
    attempts_allowed = 1 + hooks.worker_transient_retries()
    with hooks.bind_worker_agent(agent_id):
        attempt = 0
        while True:
            attempt += 1
            try:
                output: Any = (
                    worker_fn(prompt, project_scope)
                    if project_scope else worker_fn(prompt)
                )
                if project_scope:
                    output = hooks.validate_repository_result(output, project_scope)
                    if creation_worker_root and isinstance(output, RepositoryWorkerResult):
                        output = hooks.attach_creation_receipt(
                            output, creation_worker_root,
                        )
                break
            except Exception as exc:
                failure_class = hooks.classify_worker_error(exc)
                wait_seconds = hooks.retry_plan(
                    exc, attempt, attempts_allowed, failure_class,
                    TRANSIENT_FAILURE_CLASSES,
                    lambda: hooks.cancel_requested(agent_id),
                )
                legacy_retry = wait_seconds is not None
                if observe_strategy(
                    attempt, accepted=False, failure_code=failure_class,
                    legacy_retry=legacy_retry,
                ):
                    hooks.event(
                        agent_id,
                        "transient %s failure; retrying model call (attempt %d/%d)"
                        % (failure_class, attempt + 1, attempts_allowed),
                    )
                    report(adaptive_concurrency.Outcome.TRANSIENT_RETRY)
                    if hooks.retry_pause(
                        wait_seconds or 0.0,
                        lambda: hooks.cancel_requested(agent_id),
                    ):
                        return hooks.finish(agent_id) or "CANCELLED"
                    continue
                hooks.remember_transient_failure(
                    agent_id, failure_class, TRANSIENT_FAILURE_CLASSES,
                )
                final = hooks.finish(agent_id, error="[%s] %s" % (failure_class, exc))
                report(
                    adaptive_concurrency.Outcome.TRANSIENT_RETRY
                    if failure_class in TRANSIENT_FAILURE_CLASSES
                    else adaptive_concurrency.Outcome.FAILED
                )
                return final if final in ABORT_MARKERS else hooks.worker_failed_sentinel

    stored_output = (
        hooks.render_repository_result(output)
        if isinstance(output, RepositoryWorkerResult) else str(output or "")
    )
    verification_metrics: dict[str, Any] = {"worker_finished": True}
    if objectives:
        assert pre_call_metrics is not None
        post_call = hooks.validate_delegation(
            master_task,
            prompt,
            objectives,
            expected_master_digest=master_task_digest,
            expected_delegated_digest=delegated_task_digest,
            project=project_scope,
            expected_target_digests=pre_call_metrics["target_digests"],
        )
        post_call["phase"] = "post_call"
        if post_call["task_drift"]:
            hooks.finish(
                agent_id,
                error="delegated target changed during model call",
                task_drift=True,
                drift_metrics=post_call,
            )
            observe_strategy(attempt, accepted=False, failure_code="task_drift")
            report(adaptive_concurrency.Outcome.CHURN)
            return hooks.worker_failed_sentinel
        verification_metrics = hooks.validate_result(
            stored_output, objectives, project=project_scope,
        )
        if verification_metrics["task_drift"]:
            hooks.finish(
                agent_id,
                error="delegated result missed authoritative objective coverage",
                task_drift=True,
                drift_metrics=verification_metrics,
            )
            observe_strategy(attempt, accepted=False, failure_code="task_drift")
            report(adaptive_concurrency.Outcome.FAILED)
            return hooks.worker_failed_sentinel

    final = hooks.finish(agent_id, output=stored_output)
    observe_strategy(
        attempt,
        accepted=final not in ABORT_MARKERS,
        failure_code="cancelled" if final in ABORT_MARKERS else "",
    )
    if final in ABORT_MARKERS:
        return final
    report(adaptive_concurrency.Outcome.SUCCEEDED)
    completed: Any = output if isinstance(output, RepositoryWorkerResult) else final
    if not run_id:
        return completed
    verifier_receipt = readiness_verifier_receipt(
        agent_id,
        run_id,
        delegated_task_digest,
        stored_output,
        verification_metrics,
        task_digest_fn=hooks.task_digest,
    )
    return ReadyWorkerOutput(
        output=completed,
        readiness=ArtifactReadiness.from_content(
            agent_id,
            run_id,
            stored_output,
            source_revision=delegated_task_digest,
            verifier_receipt=verifier_receipt,
        ),
    )


def execute_inline(
    task: str,
    worker_fn: Callable[..., Any],
    *,
    hooks: InlineExecutionHooks,
    metadata: Mapping[str, Any] | None = None,
    project: str = "",
) -> dict[str, Any]:
    """Run one master task inline with provenance validation and scope binding."""
    objectives = hooks.parse_objectives(task)
    repository_task = (
        bool(objectives)
        or requires_repository_tools(task)
        or bool(str(project or "").strip())
    )
    project_scope = (
        hooks.resolve_repository_project_root(task, project) if repository_task else ""
    )
    meta = dict(metadata or {})
    meta.setdefault("mode", "inline")
    digest = hooks.task_digest(task)
    execution_task = task
    if objectives:
        execution_task = "%s\n\n=== AUTHORITATIVE MASTER TASK ===\n%s" % (
            hooks.objective_contract(objectives), task,
        )
    delegated_digest = hooks.task_digest(execution_task)
    meta["master_task_digest"] = digest
    meta["delegated_task_digest"] = delegated_digest
    meta["objective_ids"] = [objective.objective_id for objective in objectives]
    if project_scope:
        meta["project"] = project_scope
    master_id = hooks.new_agent("master", task, metadata=meta)
    start_activity = (
        "validating inline task provenance" if objectives
        else "running inline as master"
    )
    if not hooks.start_agent(master_id, start_activity, tool_calls=1):
        return {"mode": "inline", "master_id": master_id, "output": "CANCELLED"}
    pre_call_metrics: dict[str, Any] | None = None
    if objectives:
        pre_call_metrics = hooks.validate_delegation(
            task,
            execution_task,
            objectives,
            expected_master_digest=digest,
            expected_delegated_digest=delegated_digest,
            project=project_scope,
        )
        if pre_call_metrics["task_drift"]:
            hooks.finish(
                master_id,
                error="inline task provenance changed before model call",
                task_drift=True,
                drift_metrics=pre_call_metrics,
            )
            return {
                "mode": "inline",
                "master_id": master_id,
                "output": "TASK_DRIFT: inline objective validation failed",
                "task_drift": True,
                "drift_metrics": pre_call_metrics,
            }
        if not hooks.begin_model_call(
            master_id,
            "calling model for provenance-validated inline task",
            tool_calls=1,
        ):
            hooks.finish(master_id)
            return {"mode": "inline", "master_id": master_id, "output": "CANCELLED"}
    else:
        if not hooks.begin_model_call(
            master_id, "running inline as master", tool_calls=1,
        ):
            hooks.finish(master_id)
            return {"mode": "inline", "master_id": master_id, "output": "CANCELLED"}

    with hooks.bind_worker_agent(master_id):
        try:
            output: Any = (
                worker_fn(execution_task, project_scope)
                if project_scope else worker_fn(execution_task)
            )
            if project_scope:
                output = hooks.validate_repository_result(output, project_scope)
        except Exception as exc:
            final = hooks.finish(master_id, error=str(exc))
            return {
                "mode": "inline",
                "master_id": master_id,
                "output": final if final in ABORT_MARKERS else "ERROR: %s" % exc,
            }
    stored_output = (
        hooks.render_repository_result(output)
        if isinstance(output, RepositoryWorkerResult) else str(output or "")
    )
    if objectives:
        assert pre_call_metrics is not None
        post_call = hooks.validate_delegation(
            task,
            execution_task,
            objectives,
            expected_master_digest=digest,
            expected_delegated_digest=delegated_digest,
            project=project_scope,
            expected_target_digests=pre_call_metrics["target_digests"],
        )
        post_call["phase"] = "post_call"
        result_metrics = hooks.validate_result(
            stored_output, objectives, project=project_scope,
        )
        metrics = post_call if post_call["task_drift"] else result_metrics
        if metrics["task_drift"]:
            hooks.finish(
                master_id,
                error="inline result missed authoritative objective coverage",
                task_drift=True,
                drift_metrics=metrics,
            )
            return {
                "mode": "inline",
                "master_id": master_id,
                "output": "TASK_DRIFT: inline result failed objective validation",
                "task_drift": True,
                "drift_metrics": metrics,
            }
    final = hooks.finish(master_id, output=stored_output)
    return {"mode": "inline", "master_id": master_id, "output": final}


def execute_delegated(
    task: str,
    worker_fn: Callable[..., Any],
    audit_fn: Callable[[str], str],
    *,
    hooks: DelegatedExecutionHooks,
    agents: int = 3,
    metadata: Mapping[str, Any] | None = None,
    on_started: Callable[[dict[str, Any]], Any] | None = None,
    project: str = "",
    worker_cap: int | str | None = None,
    strategy_observer: Callable[..., bool] | None = None,
    build_workspace: bool = False,
) -> dict[str, Any]:
    """Run delegated multi-worker fan-out, artifact readiness barrier, and audit synthesis."""
    objectives = hooks.parse_objectives(task)
    greenfield_build = (
        bool(build_workspace) and not str(project or "").strip() and not objectives
    )
    if greenfield_build and requires_repository_tools(task):
        raise ValueError("build workspace cannot replace required repository scope")
    repository_task = (
        bool(objectives)
        or requires_repository_tools(task)
        or bool(str(project or "").strip())
        or greenfield_build
    )
    project_scope = (
        hooks.resolve_repository_project_root(task, project)
        if repository_task and not greenfield_build else ""
    )
    explicit_cap, _cap_error = (
        positive_int(worker_cap) if worker_cap is not None else (None, "")
    )
    if explicit_cap:
        agents_value, _agents_error = positive_int(agents)
        agent_count = min(agents_value or explicit_cap, hooks.explicit_agent_ceiling())
    else:
        agent_count = hooks.clamp_agent_count(agents, default=3)
    capacity_data = hooks.capacity(agent_count, worker_cap=worker_cap)
    worker_slots = int(
        hooks.parallel_worker_slots(agent_count, worker_cap=worker_cap)
        if worker_cap is not None else hooks.parallel_worker_slots(agent_count)
    )
    meta = dict(metadata or {})
    meta.setdefault("mode", "delegated")
    meta["requested_agents"] = agent_count
    meta["worker_slots"] = worker_slots
    if project_scope:
        meta["project"] = project_scope
    master_digest = hooks.task_digest(task)
    meta["master_task_digest"] = master_digest
    meta["delegated_task_digest"] = master_digest
    meta["objective_ids"] = [objective.objective_id for objective in objectives]
    master_id = hooks.new_agent("master", task, metadata=meta)
    creation: Any = None
    worker_projects: tuple[str, ...] = ()
    build_details: dict[str, Any] = {}
    if greenfield_build:
        try:
            creation = hooks.create_workspace(master_id, agent_count)
        except Exception as exc:
            hooks.finish(master_id, error="build workspace provisioning failed: %s" % exc)
            raise
        worker_projects = tuple(str(path) for path in creation.workers)
        build_details = {"output_workspace": str(creation.root)}
        hooks.event(master_id, "build output workspace: %s" % creation.root)

    try:
        schedule = "queued %d agent(s) across %d worker slot(s)" % (
            agent_count, worker_slots,
        )
        if capacity_data.get("source") == "per-run worker_cap":
            schedule += " [per-run worker_cap=%d; operator ceiling=%d]" % (
                capacity_data["requested_worker_cap"],
                capacity_data["operator_worker_ceiling"],
            )
        started = hooks.start_agent(
            master_id,
            schedule,
            requested_agents=agent_count,
            worker_slots=worker_slots,
        )
        if not started:
            result: dict[str, Any] = {
                "mode": "delegated",
                "master_id": master_id,
                "agents": [],
                "worker_slots": worker_slots,
                **build_details,
                "outputs": [],
                "output": "CANCELLED",
            }
            if on_started is not None:
                on_started(dict(result))
            return result

        assignments = objective_assignments(objectives, agent_count)
        prompts = hooks.subtask_prompts(
            task,
            agent_count,
            tool_access=repository_task,
            project=project_scope,
            objective_assignments=assignments,
            build_projects=worker_projects,
        )
        child_ids: list[str] = []
        try:
            padded_assignments: Sequence[Sequence[Any]] = assignments or [
                () for _ in prompts
            ]
            for prompt, assigned in zip(prompts, padded_assignments, strict=False):
                child_ids.append(
                    hooks.new_agent(
                        "agent",
                        prompt,
                        parent_id=master_id,
                        metadata={
                            "project": project_scope,
                            "master_task_digest": master_digest,
                            "delegated_task_digest": hooks.task_digest(prompt),
                            "objective_ids": [
                                objective.objective_id for objective in assigned
                            ],
                        },
                    )
                )
            lane_claims = delegated_lane_claims(
                child_ids, assignments, project_root=project_scope,
            )
        except Exception as exc:
            error = "fleet startup failed after queueing %d of %d delegated agents: %s" % (
                len(child_ids), agent_count, exc,
            )
            for queued_id in child_ids:
                with contextlib.suppress(Exception):
                    hooks.request_cancel(queued_id)
            final = hooks.finish(master_id, error=error)
            result = {
                "mode": "delegated",
                "master_id": master_id,
                "agents": list(child_ids),
                "worker_slots": worker_slots,
                **build_details,
                "outputs": [],
                "output": final if final in ABORT_MARKERS else "ERROR: %s" % error,
            }
            if on_started is not None:
                on_started(dict(result))
            return result

        if on_started is not None:
            on_started({
                "mode": "delegated",
                "master_id": master_id,
                "agents": list(child_ids),
                "worker_slots": worker_slots,
                **build_details,
                "outputs": [],
                "output": "RUNNING",
            })

        outputs: list[tuple[str, Any]] = []
        readiness_by_producer: dict[str, ArtifactReadiness] = {}
        readiness_errors: dict[str, str] = {}
        fanout_started = time.monotonic()
        padded_lane_assignments: Sequence[Sequence[Any]] = assignments or [
            () for _ in prompts
        ]
        lane_inputs: dict[str, tuple[str, Sequence[Any]]] = {
            agent_id: (prompt, assigned)
            for agent_id, prompt, assigned in zip(
                child_ids, prompts, padded_lane_assignments, strict=False
            )
        }
        lane_projects: dict[str, str] = {
            agent_id: (worker_projects[index] if greenfield_build else project_scope)
            for index, agent_id in enumerate(child_ids)
        }

        def _concurrency_event(record: dict[str, Any]) -> None:
            hooks.event(
                master_id,
                "adaptive concurrency %s: cap %d -> %d (%s)" % (
                    record["action"], record["previous_cap"], record["cap"],
                    ",".join(record["reasons"]) or "none",
                ),
            )

        scheduler = hooks.scheduler_cls(
            lane_claims,
            worker_slots,
            adaptive=hooks.adaptive_concurrency_enabled(),
            on_decision=_concurrency_event,
        )

        def _run_lane(
            agent_id: str,
            sink: Callable[[adaptive_concurrency.Outcome], None],
        ) -> Any:
            prompt, assigned = lane_inputs[agent_id]
            return hooks.run_worker(
                agent_id,
                prompt,
                worker_fn,
                lane_projects[agent_id],
                task,
                assigned,
                master_digest,
                hooks.task_digest(prompt),
                master_id,
                outcome_sink=sink,
                strategy_observer=strategy_observer,
                model_route=str(meta.get("tier") or ""),
                creation_worker_root=lane_projects[agent_id] if greenfield_build else "",
            )

        def _collect(agent_id: str, output: Any) -> None:
            if output not in ABORT_MARKERS and output is not hooks.worker_failed_sentinel:
                if isinstance(output, ReadyWorkerOutput):
                    readiness_by_producer[agent_id] = output.readiness
                    output = output.output
                else:
                    readiness_errors[agent_id] = "missing producer readiness evidence"
                outputs.append((agent_id, output))

        def _lane_error(agent_id: str, exc: BaseException) -> None:
            hooks.finish(agent_id, error=str(exc))

        def _lane_abandoned(agent_id: str) -> None:
            try:
                hooks.finish(agent_id, error="fleet dispatch could not complete this lane")
            except Exception:
                hooks.release_reservation(agent_id)
                raise

        try:
            hooks.dispatch_lanes(
                scheduler,
                worker_slots,
                _run_lane,
                _collect,
                _lane_error,
                on_abandon=_lane_abandoned,
                on_stall=lambda lanes: [hooks.request_cancel(lane) for lane in lanes],
            )
        except FleetStalledError as exc:
            concurrency_report = scheduler.summary()
            error = "fleet stalled; result is uncertain: %s" % exc
            hooks.finish(master_id, error=error)
            return {
                "mode": "delegated",
                "master_id": master_id,
                "agents": child_ids,
                "worker_slots": worker_slots,
                **build_details,
                "concurrency": concurrency_report,
                "outputs": hooks.public_outputs(outputs),
                "output": "STALLED: %s" % error,
                "stalled": True,
                "uncertain": True,
                "stalled_lanes": list(exc.lanes),
            }

        concurrency_report = scheduler.summary()
        if hooks.cancel_requested(master_id):
            final = hooks.finish(master_id)
            return {
                "mode": "delegated",
                "master_id": master_id,
                "agents": child_ids,
                "worker_slots": worker_slots,
                **build_details,
                "concurrency": concurrency_report,
                "outputs": hooks.public_outputs(outputs),
                "output": final,
            }

        if not outputs:
            if objectives:
                aggregation = hooks.aggregation_metrics(
                    objectives, (), len(child_ids),
                )
                hooks.finish(
                    master_id,
                    error="aggregation refused: every child missed objective evidence",
                    task_drift=True,
                    drift_metrics=aggregation,
                )
                return {
                    "mode": "delegated",
                    "master_id": master_id,
                    "agents": child_ids,
                    "worker_slots": worker_slots,
                    **build_details,
                    "concurrency": concurrency_report,
                    "outputs": [],
                    "output": "TASK_DRIFT: all delegated results missed objective evidence",
                    "task_drift": True,
                    "drift_metrics": aggregation,
                }
            capacity_error = hooks.capacity_unavailable_error(repository_task, child_ids)
            if repository_task and not capacity_error:
                report_text = (
                    hooks.build_report(creation.root, lane_projects, ())
                    if creation else ""
                )
                final = hooks.finish(master_id, output=report_text + EVIDENCE_REQUIRED)
                return {
                    "mode": "delegated",
                    "master_id": master_id,
                    "agents": child_ids,
                    "worker_slots": worker_slots,
                    **build_details,
                    "concurrency": concurrency_report,
                    "outputs": [],
                    "output": final,
                }
            error = capacity_error or (
                "all delegated workers failed before producing an auditable result"
            )
            final = hooks.finish(master_id, error=error)
            return {
                "mode": "delegated",
                "master_id": master_id,
                "agents": child_ids,
                "worker_slots": worker_slots,
                **build_details,
                "concurrency": concurrency_report,
                "outputs": [],
                "output": final if final in ABORT_MARKERS else "ERROR: %s" % error,
            }

        rendered_outputs = {
            agent_id: (
                hooks.render_repository_result(output)
                if isinstance(output, RepositoryWorkerResult) else str(output or "")
            )
            for agent_id, output in outputs
        }
        readiness_age_seconds = min(
            24 * 60 * 60,
            max(15 * 60, time.monotonic() - fanout_started + 5 * 60),
        )
        readiness_errors.update(
            validate_master_slots_for_run(
                run_id=master_id,
                expected_producers=child_ids,
                readiness_by_producer=readiness_by_producer,
                content_by_producer=rendered_outputs,
                source_revisions={
                    agent_id: hooks.task_digest(lane_inputs[agent_id][0])
                    for agent_id in child_ids
                },
                require_verifier_receipt={agent_id: True for agent_id in child_ids},
                expected_verifier_receipts={
                    agent_id: readiness_verifier_receipt(
                        agent_id,
                        master_id,
                        hooks.task_digest(lane_inputs[agent_id][0]),
                        rendered_outputs[agent_id],
                        hooks.validate_result(
                            rendered_outputs[agent_id],
                            lane_inputs[agent_id][1],
                            project=project_scope,
                        )
                        if lane_inputs[agent_id][1]
                        else {"worker_finished": True},
                        task_digest_fn=hooks.task_digest,
                    )
                    for agent_id in rendered_outputs
                },
                max_age=timedelta(seconds=readiness_age_seconds),
            )
        )
        no_valid = all(agent_id in readiness_errors for agent_id, _output in outputs)
        if no_valid or (
            repository_task
            and any(
                not isinstance(output, RepositoryWorkerResult)
                or not hooks.same_project_root(output.project, lane_projects[agent_id])
                for agent_id, output in outputs
                if agent_id not in readiness_errors
            )
        ):
            error = (
                "artifact readiness barrier rejected fan-in: no validated child artifact"
                if no_valid
                else "repository aggregation rejected an unscoped child result"
            )
            final = hooks.finish(master_id, error=error)
            return {
                "mode": "delegated",
                "master_id": master_id,
                "agents": child_ids,
                "worker_slots": worker_slots,
                **build_details,
                "concurrency": concurrency_report,
                "outputs": hooks.public_outputs(outputs) if no_valid else [],
                "output": final if final in ABORT_MARKERS else "ERROR: %s" % error,
            }

        valid_outputs = [
            (agent_id, output)
            for agent_id, output in outputs
            if agent_id not in readiness_errors
        ]
        valid_assignments: Sequence[Sequence[Any]] = (
            [assignments[child_ids.index(agent_id)] for agent_id, _ in valid_outputs]
            if assignments
            else [() for _ in valid_outputs]
        )
        child_metrics = [
            hooks.validate_result(
                hooks.render_repository_result(output)
                if isinstance(output, RepositoryWorkerResult) else str(output or ""),
                assigned,
                project=project_scope,
            )
            for (_agent_id, output), assigned in zip(
                valid_outputs, valid_assignments, strict=False
            )
        ]
        aggregation_result: dict[str, Any] = {}
        if objectives:
            aggregation_result = hooks.aggregation_metrics(
                objectives, child_metrics, len(child_ids),
            )
            if aggregation_result["task_drift"]:
                hooks.finish(
                    master_id,
                    error="aggregation refused: authoritative objective coverage drifted",
                    task_drift=True,
                    drift_metrics=aggregation_result,
                )
                return {
                    "mode": "delegated",
                    "master_id": master_id,
                    "agents": child_ids,
                    "worker_slots": worker_slots,
                    **build_details,
                    "concurrency": concurrency_report,
                    "outputs": hooks.public_outputs(outputs),
                    "output": "TASK_DRIFT: aggregation refused due to missing objective evidence",
                    "task_drift": True,
                    "drift_metrics": aggregation_result,
                }

        if not hooks.begin_model_call(
            master_id, "auditing delegated outputs", tool_calls=2,
        ):
            final = hooks.finish(master_id)
            return {
                "mode": "delegated",
                "master_id": master_id,
                "agents": child_ids,
                "worker_slots": worker_slots,
                **build_details,
                "concurrency": concurrency_report,
                "outputs": hooks.public_outputs(outputs),
                "output": final,
            }

        build_report_text = (
            hooks.build_report(creation.root, lane_projects, valid_outputs)
            if creation is not None else ""
        )
        prompt_pairs: Iterable[tuple[str, str]] = (
            (
                agent_id,
                "[ARTIFACT REJECTED: %s; output withheld from synthesis]"
                % readiness_errors[agent_id]
                if agent_id in readiness_errors
                else rendered_outputs[agent_id],
            )
            for agent_id, _output in outputs
        )
        audit_prompt_text = hooks.audit_prompt(
            task,
            prompt_pairs,
            repository_task=repository_task,
            project=project_scope,
            objective_contract=(
                hooks.objective_contract(objectives) if objectives else ""
            ),
            creation_root=str(creation.root) if creation is not None else "",
        )
        if build_report_text:
            audit_prompt_text += "\n" + build_report_text

        with hooks.bind_worker_agent(master_id):
            try:
                merged = audit_fn(audit_prompt_text)
            except Exception as exc:
                merged = build_report_text + "ERROR: audit failed: %s" % exc
                final = hooks.finish(master_id, output=merged, error=str(exc))
                return {
                    "mode": "delegated",
                    "master_id": master_id,
                    "agents": child_ids,
                    "worker_slots": worker_slots,
                    **build_details,
                    "concurrency": concurrency_report,
                    "outputs": hooks.public_outputs(outputs),
                    "output": final if final in ABORT_MARKERS else merged,
                }
        if repository_task and not greenfield_build:
            merged = (
                "=== HOST AGGREGATION SCOPE ===\nproject=%s\nchildren=%s\n\n%s"
                % (
                    project_scope,
                    ",".join(agent_id for agent_id, _ in valid_outputs),
                    merged,
                )
            )
        merged = build_report_text + merged
        if objectives:
            aggregate_metrics = hooks.validate_aggregate_output(
                merged, objectives, aggregation_result,
            )
            if aggregate_metrics["task_drift"]:
                hooks.finish(
                    master_id,
                    error="audit aggregate drifted from authoritative objectives",
                    task_drift=True,
                    drift_metrics=aggregate_metrics,
                )
                return {
                    "mode": "delegated",
                    "master_id": master_id,
                    "agents": child_ids,
                    "worker_slots": worker_slots,
                    **build_details,
                    "concurrency": concurrency_report,
                    "outputs": hooks.public_outputs(outputs),
                    "output": "TASK_DRIFT: audit aggregate omitted authoritative objectives",
                    "task_drift": True,
                    "drift_metrics": aggregate_metrics,
                }
        final = hooks.finish(master_id, output=merged)
        return {
            "mode": "delegated",
            "master_id": master_id,
            "agents": child_ids,
            "worker_slots": worker_slots,
            **build_details,
            "concurrency": concurrency_report,
            "outputs": hooks.public_outputs(outputs),
            "output": final,
        }
    finally:
        if creation is not None:
            hooks.release_workspace(creation)


def start_delegated_background(
    *,
    run_delegated_fn: Callable[..., dict[str, Any]],
    request_cancel_fn: Callable[[str], Any],
    finish_fn: Callable[..., str],
    thread_cls: Callable[..., Any],
    task: str,
    worker_fn: Callable[..., Any],
    audit_fn: Callable[[str], str],
    agents: int = 3,
    metadata: Mapping[str, Any] | None = None,
    startup_timeout: float = 5.0,
    project: str = "",
    worker_cap: int | str | None = None,
    strategy_observer: Callable[..., bool] | None = None,
    build_workspace: bool = False,
) -> dict[str, Any]:
    """Start delegated orchestration in a daemon thread and return ledger IDs."""
    ready = threading.Event()
    abandoned = threading.Event()
    started_result: dict[str, Any] = {}
    startup_error: list[BaseException] = []

    def on_started(result: dict[str, Any]) -> None:
        started_result.update(result)
        ready.set()
        if abandoned.is_set():
            master_id = result.get("master_id")
            if master_id:
                with contextlib.suppress(Exception):
                    request_cancel_fn(master_id)

    def run() -> None:
        try:
            run_delegated_fn(
                task,
                worker_fn=worker_fn,
                audit_fn=audit_fn,
                agents=agents,
                metadata=metadata,
                _on_started=on_started,
                project=project,
                worker_cap=worker_cap,
                strategy_observer=strategy_observer,
                build_workspace=build_workspace,
            )
        except Exception as exc:
            master_id = started_result.get("master_id")
            if master_id:
                with contextlib.suppress(Exception):
                    finish_fn(master_id, error=str(exc))
            else:
                startup_error.append(exc)
            ready.set()

    thread = thread_cls(
        target=run,
        name="sonder-master-background",
        daemon=True,
    )
    thread.start()
    if not ready.wait(max(0.1, float(startup_timeout))):
        abandoned.set()
        master_id = started_result.get("master_id")
        if master_id:
            with contextlib.suppress(Exception):
                request_cancel_fn(master_id)
        raise RuntimeError(
            "background fleet did not initialize its durable ledger; "
            "any late startup will be cancelled instead of running as an orphan"
        )
    if startup_error:
        raise RuntimeError(
            "background fleet failed to start: %s" % startup_error[0]
        )
    result = dict(started_result)
    result["background"] = True
    return result
