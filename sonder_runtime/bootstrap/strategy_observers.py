"""Bounded translations from host outcomes to observe-only strategy facts.

Only identities, digest references and numeric progress enter the sealed
checkpoint. These adapters neither execute controller suggestions nor infer
effect replay safety from a model response or an error string.
"""
from __future__ import annotations

from dataclasses import fields, replace
import hashlib
import json

from sonder_runtime.domain.strategy.models import (
    EvidenceRef,
    FailureClass,
    FailureObservation,
    ProgressMetric,
    ProgressVector,
    StrategyAction,
    StrategyAttempt,
    StrategyBudget,
    StrategySignature,
    StrategyUsage,
)


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True,
                                     default=str).encode("ascii")).hexdigest()


# Per-attempt clamp for host counters copied into sealed usage. Values above
# it are recorded at the clamp; the strategy budgets below are sized from it.
_COUNTER_CLAMP = 64


def _counter(value) -> int:
    """Bound a host-owned integer counter; anything else counts as zero."""
    if type(value) is not int or value < 0:
        return 0
    return min(value, _COUNTER_CLAMP)


def _within_sealed(trace, run_id: str, requested: StrategyBudget) -> StrategyBudget:
    """Never request more than the budget a run was already sealed with."""
    sealed = trace.sealed_budget(run_id)
    if sealed is None:
        return requested
    return StrategyBudget(**{
        item.name: min(getattr(requested, item.name), getattr(sealed, item.name))
        for item in fields(StrategyBudget)
    })


def _attributes_counters(trace, run_id: str, requested: StrategyBudget) -> bool:
    """Whether a run's new attempts may be charged host counters.

    A run sealed before counters were attributed carries the default budget,
    which is narrower than the attributed request in the counted dimensions.
    Its earlier attempts were charged nothing, so charging the next attempt
    lifetime counters would bill it for work already observed and could
    exhaust the narrowed legacy budget. Such a run keeps charging attempts
    only; a run sealed under the attributed budget, or not sealed yet, is
    charged its counters.
    """
    sealed = trace.sealed_budget(run_id)
    return sealed is None or sealed.allows(requested)


def _as_sealed(attempt: StrategyAttempt, history) -> StrategyAttempt:
    """Keep the charge and progress of an attempt that is already sealed.

    Usage and progress are fixed when an attempt is first recorded. A replay
    still re-derives identity, outcome, failure, evidence and route, and the
    trace rejects any difference in those; it never re-prices the sealed
    attempt, so checkpoints written before a counter was attributed remain
    replayable.
    """
    for sealed in history:
        if sealed.attempt_id == attempt.attempt_id:
            return replace(attempt, usage=sealed.usage,
                           progress_before=sealed.progress_before,
                           progress_after=sealed.progress_after)
    return attempt


def codegen_objective_digest(project_dir: str, spec: str, build_program: str) -> str:
    return _digest((project_dir, spec, build_program))


def codegen_attempt_id(file_name: str, attempt_number: int) -> str:
    return f"file-{_digest(file_name)[:12]}-attempt-{attempt_number}"


def observe_codegen_build(trace, *, run_id: str, file_name: str, project_dir: str,
                          spec: str, build_program: str, attempt_number: int,
                          attempt_limit: int, code: str, before_errors: list[str],
                          after_errors: list[str], before_complete: bool,
                          after_complete: bool, build_ran: bool, exit_ok: bool,
                          route: str, critic_used: bool = False,
                          rotated: bool = False, rejected: bool = False,
                          unresolved_effects: bool = False, memory_service=None,
                          no_progress: bool = False, available_actions=None,
                          reserved_usage: StrategyUsage | None = None,
                          reserved_budget: StrategyBudget | None = None,
                          reserved_action: StrategyAction | None = None):
    """Observe one codegen candidate after its build or a pre-write rejection."""
    scope = _digest((project_dir, file_name, build_program))

    def vector(errors, complete):
        return ProgressVector(scope, (ProgressMetric("compiler_errors", len(errors)),),
                              complete=bool(complete))

    if rejected:
        failure = FailureObservation(FailureClass.HYPOTHESIS_REJECTED,
                                     "SHRINK_REJECTED", _digest(code))
    elif unresolved_effects:
        failure = FailureObservation(FailureClass.UNCERTAIN_SIDE_EFFECT,
                                     "WRITE_OUTCOME_UNKNOWN", _digest(code))
    elif not build_ran:
        failure = FailureObservation(FailureClass.ENVIRONMENT_FAILURE,
                                     "BUILD_DID_NOT_RUN", _digest(after_errors))
    elif not after_complete:
        failure = FailureObservation(FailureClass.VERIFIER_FAILURE,
                                     "BUILD_MEASUREMENT_INCOMPLETE", _digest(after_errors))
    elif not exit_ok or after_errors:
        failure = FailureObservation(
                                     FailureClass.NO_PROGRESS if no_progress else FailureClass.BUILD_FAILURE,
                                     "BUILD_NO_PROGRESS" if no_progress else "BUILD_FAILED",
                                     _digest(after_errors))
    else:
        failure = None
    signature = StrategySignature(
        "patch", codegen_objective_digest(project_dir, spec, build_program),
        ("project:" + scope,), _digest(code),
        "repair compiler-checked file", "build:" + _digest(build_program)[:16],
    )
    attempt = StrategyAttempt(
        run_id, codegen_attempt_id(file_name, attempt_number),
        signature, "uncertain" if unresolved_effects else "failed" if failure else "succeeded",
        failure, vector(before_errors, before_complete),
        vector(after_errors, after_complete),
        reserved_usage or StrategyUsage(
            attempts=1, model_calls=1 + int(critic_used),
            verifier_calls=0 if rejected or unresolved_effects else 1,
            critic_calls=int(critic_used), strategy_switches=int(rotated),
        ),
        model_route=route[:128],
    )
    action_choices = available_actions if available_actions is not None else (
        StrategyAction.REPAIR, StrategyAction.INSPECT,
        StrategyAction.CRITIC, StrategyAction.SWITCH_MODEL,
    )
    recorder = trace.record_reserved if reserved_usage is not None else trace.record
    decision = recorder(
        attempt, budget=reserved_budget or StrategyBudget(attempts=attempt_limit),
        available_actions=action_choices,
        unresolved_effects=unresolved_effects,
        transport_replay_safe=False,
        **({"action": reserved_action} if reserved_usage is not None else {}),
    )
    if memory_service is not None and attempt.outcome in {"succeeded", "failed"}:
        from sonder_runtime.application.memory.strategy_memory import language_from_path

        memory_service.observe_recorded(
            attempt.run_id, attempt.attempt_id, project_scope=project_dir,
            language=language_from_path(file_name),
        )
    return decision


_TASK_FAMILIES = {
    "inspect": "inspect", "research": "retrieve", "implement": "patch",
    "validate": "diagnose", "report": "diagnose",
}


def observe_workbench_lane(trace, *, lane: dict, memory_service=None):
    """Observe a durable terminal lane attempt without authorizing replay."""
    status = lane.get("status")
    if status not in {"completed", "failed", "awaiting_input"}:
        return None
    uncertain = bool(lane.get("pending_effect")) or status == "awaiting_input"
    run_id = str(lane["id"])
    attempt_id = str(lane["attempt_id"])
    project_scope = str(lane["workspace_root"])
    scope = _digest((run_id, project_scope))
    error_code = str(lane.get("error") or "")
    if uncertain:
        failure_class = FailureClass.UNCERTAIN_SIDE_EFFECT
        source_code = "LANE_EFFECT_UNRESOLVED"
    elif status == "failed":
        failure_class = (
            FailureClass.PERMISSION_DENIED if error_code == "AUTHORITY_DENIED" else
            FailureClass.TIME_BUDGET if error_code == "BUDGET_EXHAUSTED" else
            FailureClass.CONTEXT_EXHAUSTION if error_code == "CONTEXT_HISTORY_OVERFLOW" else
            FailureClass.ENVIRONMENT_FAILURE
        )
        source_code = error_code if error_code in {
            "AUTHORITY_DENIED", "BUDGET_EXHAUSTED", "CONTEXT_HISTORY_OVERFLOW",
            "LANE_ATTEMPT_FAILED",
        } else "LANE_ATTEMPT_FAILED"
    else:
        failure_class = None
        source_code = ""
    failure = None if failure_class is None else FailureObservation(
        failure_class, source_code, _digest((error_code, status, uncertain)),
    )
    before = ProgressVector(
        scope, (ProgressMetric("lane_completed", 0, "maximize"),), complete=True,
    )
    after = ProgressVector(
        scope, (ProgressMetric("lane_completed", int(status == "completed"), "maximize"),),
        complete=not uncertain,
    )
    signature = StrategySignature(
        "patch", _digest((lane.get("task", ""), project_scope)),
        ("lane:" + scope,), _digest((lane.get("task", ""), lane.get("tier", ""))),
        "complete host-scoped interactive lane", "lane-completion",
    )
    # ``used_steps`` is the lane's lifetime count of dispatched model turns
    # and is stable while an attempt is terminal. This attempt is charged the
    # turns not already charged to earlier sealed attempts of the lane, so the
    # run's total model_calls equals the durable lane counter. Turns from
    # attempts that were never observed (for example an interrupted attempt)
    # are charged to the next observed attempt rather than dropped.
    step_budget = max(1, min(int(lane["max_steps"]), 64))
    requested = StrategyBudget(attempts=step_budget, model_calls=step_budget)
    history = trace.history(run_id)
    prior = history
    for index, sealed in enumerate(history):
        if sealed.attempt_id == attempt_id:
            prior = history[:index]
            break
    charged = sum(item.usage.model_calls for item in prior)
    used_steps = lane.get("used_steps")
    model_calls = (
        _counter(used_steps - charged)
        if type(used_steps) is int and _attributes_counters(trace, run_id, requested)
        else 0
    )
    attempt = _as_sealed(StrategyAttempt(
        run_id, attempt_id, signature,
        "uncertain" if uncertain else "succeeded" if status == "completed" else "failed",
        failure, before, after, StrategyUsage(attempts=1, model_calls=model_calls),
        model_route=str(lane.get("tier") or "")[:128],
    ), history)
    decision = trace.record(
        attempt, budget=_within_sealed(trace, run_id, requested),
        available_actions=(StrategyAction.INSPECT, StrategyAction.REPAIR, StrategyAction.CRITIC),
        unresolved_effects=uncertain,
        policy_blocked=failure_class is FailureClass.PERMISSION_DENIED,
        transport_replay_safe=False,
    )
    if memory_service is not None and attempt.outcome in {"succeeded", "failed"}:
        memory_service.observe_recorded(
            run_id, attempt_id, project_scope=project_scope,
        )
    return decision


_FLEET_FAILURES = {
    "timeout": FailureClass.TRANSIENT_TRANSPORT,
    "unavailable": FailureClass.TRANSIENT_TRANSPORT,
    "transport": FailureClass.TRANSIENT_TRANSPORT,
    "throttled": FailureClass.RATE_LIMIT,
    "request_rejected": FailureClass.INVALID_TOOL_ARGUMENT,
    "task_drift": FailureClass.STALE_EVIDENCE,
    "unknown": FailureClass.IMPLEMENTATION_FAILURE,
}


def observe_fleet_worker(trace, *, agent_id: str, master_id: str, prompt: str,
                         master_digest: str, project_scope: str, attempt_number: int,
                         attempt_limit: int, route: str, accepted: bool,
                         failure_code: str = "", effects_resolved: bool = False,
                         transport_replay_safe: bool = False,
                         memory_service=None):
    """Observe one delegated model call using host failure and effect facts."""
    scope = _digest((master_id, agent_id, project_scope))
    objective = _digest((master_digest, project_scope))
    signature = StrategySignature(
        "retrieve" if transport_replay_safe else "delegate", objective,
        ("fleet:" + scope,), _digest(prompt), "complete bounded delegated worker",
        "fleet-accepted-result",
    )
    unresolved = not accepted and not effects_resolved
    failure = None if accepted else FailureObservation(
        _FLEET_FAILURES.get(failure_code, FailureClass.IMPLEMENTATION_FAILURE),
        "FLEET_" + failure_code.upper() if failure_code in _FLEET_FAILURES else "FLEET_UNKNOWN",
        _digest((failure_code, attempt_number)),
    )
    before = ProgressVector(
        scope, (ProgressMetric("accepted_result", 0, "maximize"),), complete=True,
    )
    after = ProgressVector(
        scope, (ProgressMetric("accepted_result", int(accepted), "maximize"),),
        complete=accepted or effects_resolved,
    )
    attempt = StrategyAttempt(
        agent_id, f"{agent_id}-attempt-{attempt_number}", signature,
        "succeeded" if accepted else "uncertain" if unresolved else "failed",
        failure, before, after, StrategyUsage(attempts=1, model_calls=1),
        model_route=str(route or "")[:128],
    )
    decision = trace.record(
        attempt,
        budget=StrategyBudget(attempts=attempt_limit, model_calls=attempt_limit),
        available_actions=(StrategyAction.RETRY_TRANSIENT, StrategyAction.INSPECT),
        unresolved_effects=unresolved,
        transport_replay_safe=transport_replay_safe and effects_resolved,
    )
    if memory_service is not None and attempt.outcome in {"succeeded", "failed"}:
        memory_service.observe_recorded(
            agent_id, attempt.attempt_id, project_scope=project_scope,
        )
    return decision


def observe_autopilot_task(trace, *, run: dict, task: dict, memory_service=None):
    """Observe one durably saved Autopilot task outcome, including crash state."""
    status = task.get("status")
    if status not in {"passed", "failed", "uncertain"}:
        return None
    run_id = str(run["id"])
    task_id = str(task["id"])
    number = int(task.get("attempts") or 0)
    if number < 1:
        return None
    scope = _digest((run_id, task_id))
    objective = _digest((run.get("objective", ""), run.get("project", "")))
    signature = StrategySignature(
        _TASK_FAMILIES[task["kind"]], objective, ("run:" + scope,),
        _digest(task.get("instruction", "")),
        f"complete host-scoped {task['kind']} task", "autopilot-task",
    )
    # The host receipt is built by the guarded workbench from host-observed
    # facts (HostTaskResult.receipt), never from model output. Anything that
    # is not a dict, or a field with the wrong type, contributes nothing.
    receipt = task.get("host_receipt")
    if not isinstance(receipt, dict):
        receipt = {}
    # The controller assigns ``host_receipt`` only when an attempt completes
    # and keeps it while a retry runs, so an interrupted (uncertain) attempt
    # still carries the previous attempt's receipt. Its counters and
    # validation progress are not this attempt's facts and are not charged.
    counted = receipt if status != "uncertain" else {}
    before_metrics = [ProgressMetric("task_passed", 0, "maximize")]
    after_metrics = [ProgressMetric("task_passed", int(status == "passed"), "maximize")]
    if task["kind"] == "validate":
        before_metrics.append(ProgressMetric("validation_passed", 0, "maximize"))
        after_metrics.append(ProgressMetric(
            "validation_passed", int(counted.get("validation_passed") is True), "maximize",
        ))
    before = ProgressVector(scope, tuple(before_metrics), complete=True)
    after = ProgressVector(scope, tuple(after_metrics), complete=status != "uncertain")
    # Autopilot has no host tool or verifier budget of its own; these bounds
    # are the per-attempt clamps times the attempt budget, so only the
    # attempt dimension can exhaust the budget of a run charged counters.
    requested = StrategyBudget(
        attempts=50, model_calls=100, tool_calls=50 * _COUNTER_CLAMP,
        verifier_calls=50, strategy_switches=50, replans=6,
    )
    if not _attributes_counters(trace, run_id, requested):
        counted = {}
    tools = counted.get("tools")
    # The receipt lists the distinct host tools used, not every call, so
    # tool_calls is a lower bound on the task's calls. validation_attempted
    # likewise proves at least one verifier run.
    usage = StrategyUsage(
        attempts=1,
        tool_calls=_counter(len(tools)) if isinstance(tools, (list, tuple)) else 0,
        verifier_calls=int(counted.get("validation_attempted") is True),
    )
    failure = None if status == "passed" else FailureObservation(
        FailureClass.UNCERTAIN_SIDE_EFFECT if status == "uncertain" else
        FailureClass.VERIFIER_FAILURE if task["kind"] == "validate" else
        FailureClass.IMPLEMENTATION_FAILURE if task["kind"] == "implement" else
        FailureClass.DEPENDENCY_FAILURE,
        "AUTOPILOT_TASK_" + status.upper(),
        _digest((receipt, task.get("error", ""))),
    )
    evidence = ()
    certificate = receipt.get("delegated_verification")
    if isinstance(certificate, dict) and certificate.get("certificate_id"):
        evidence = (EvidenceRef("verifier", str(certificate["certificate_id"])[:512],
                                _digest(certificate)),)
    attempt = _as_sealed(StrategyAttempt(
        run_id, f"{task_id}-attempt-{number}", signature,
        "succeeded" if status == "passed" else "uncertain" if status == "uncertain" else "failed",
        failure, before, after, usage, evidence,
        model_route=str(run.get("tier", ""))[:128],
    ), trace.history(run_id))
    decision = trace.record(
        attempt, budget=_within_sealed(trace, run_id, requested),
        available_actions=(StrategyAction.INSPECT, StrategyAction.REPLAN),
        unresolved_effects=status == "uncertain", transport_replay_safe=False,
    )
    if memory_service is not None and attempt.outcome in {"succeeded", "failed"}:
        memory_service.observe_recorded(
            attempt.run_id, attempt.attempt_id,
            project_scope=str(run.get("project") or "run:" + run_id),
            complete_selection=receipt.get("pre_model_context_response_observed") is True,
        )
    return decision


__all__ = [
    "codegen_attempt_id",
    "codegen_objective_digest",
    "observe_autopilot_task",
    "observe_codegen_build",
    "observe_fleet_worker",
    "observe_workbench_lane",
]
