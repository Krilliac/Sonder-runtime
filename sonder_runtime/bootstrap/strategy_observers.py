"""Bounded translations from host outcomes to observe-only strategy facts.

Only identities, digest references and numeric progress enter the sealed
checkpoint. These adapters neither execute controller suggestions nor infer
effect replay safety from a model response or an error string.
"""
from __future__ import annotations

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


def observe_codegen_build(trace, *, run_id: str, file_name: str, project_dir: str,
                          spec: str, build_program: str, attempt_number: int,
                          attempt_limit: int, code: str, before_errors: list[str],
                          after_errors: list[str], before_complete: bool,
                          after_complete: bool, build_ran: bool, exit_ok: bool,
                          route: str, critic_used: bool = False,
                          rotated: bool = False, rejected: bool = False,
                          unresolved_effects: bool = False, memory_service=None):
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
        failure = FailureObservation(FailureClass.BUILD_FAILURE,
                                     "BUILD_FAILED", _digest(after_errors))
    else:
        failure = None
    signature = StrategySignature(
        "patch", _digest((project_dir, spec, build_program)),
        ("project:" + scope,), _digest(code),
        "repair compiler-checked file", "build:" + _digest(build_program)[:16],
    )
    attempt = StrategyAttempt(
        run_id, f"file-{_digest(file_name)[:12]}-attempt-{attempt_number}",
        signature, "uncertain" if unresolved_effects else "failed" if failure else "succeeded",
        failure, vector(before_errors, before_complete),
        vector(after_errors, after_complete),
        StrategyUsage(attempts=1, model_calls=1 + int(critic_used),
                      verifier_calls=0 if rejected or unresolved_effects else 1,
                      critic_calls=int(critic_used), strategy_switches=int(rotated)),
        model_route=route[:128],
    )
    decision = trace.record(
        attempt, budget=StrategyBudget(attempts=attempt_limit),
        available_actions=(StrategyAction.REPAIR, StrategyAction.INSPECT,
                           StrategyAction.CRITIC, StrategyAction.SWITCH_MODEL),
        unresolved_effects=unresolved_effects,
        transport_replay_safe=False,
    )
    if memory_service is not None and attempt.outcome in {"succeeded", "failed"}:
        memory_service.observe_recorded(
            attempt.run_id, attempt.attempt_id, project_scope=project_dir,
        )
    return decision


_TASK_FAMILIES = {
    "inspect": "inspect", "research": "retrieve", "implement": "patch",
    "validate": "diagnose", "report": "diagnose",
}


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
    before = ProgressVector(scope, (ProgressMetric("task_passed", 0, "maximize"),),
                            complete=True)
    after = ProgressVector(scope, (ProgressMetric("task_passed", int(status == "passed"),
                                                "maximize"),), complete=status != "uncertain")
    receipt = task.get("host_receipt") or {}
    failure = None if status == "passed" else FailureObservation(
        FailureClass.UNCERTAIN_SIDE_EFFECT if status == "uncertain" else
        FailureClass.VERIFIER_FAILURE if task["kind"] == "validate" else
        FailureClass.IMPLEMENTATION_FAILURE if task["kind"] == "implement" else
        FailureClass.DEPENDENCY_FAILURE,
        "AUTOPILOT_TASK_" + status.upper(),
        _digest((receipt, task.get("error", ""))),
    )
    evidence = ()
    certificate = receipt.get("delegated_verification") if isinstance(receipt, dict) else None
    if isinstance(certificate, dict) and certificate.get("certificate_id"):
        evidence = (EvidenceRef("verifier", str(certificate["certificate_id"])[:512],
                                _digest(certificate)),)
    attempt = StrategyAttempt(
        run_id, f"{task_id}-attempt-{number}", signature,
        "succeeded" if status == "passed" else "uncertain" if status == "uncertain" else "failed",
        failure, before, after, StrategyUsage(attempts=1), evidence,
        model_route=str(run.get("tier", ""))[:128],
    )
    decision = trace.record(
        attempt, budget=StrategyBudget(attempts=50, model_calls=100,
                                       strategy_switches=50, replans=6),
        available_actions=(StrategyAction.INSPECT, StrategyAction.REPLAN),
        unresolved_effects=status == "uncertain", transport_replay_safe=False,
    )
    if memory_service is not None and attempt.outcome in {"succeeded", "failed"}:
        memory_service.observe_recorded(
            attempt.run_id, attempt.attempt_id,
            project_scope=str(run.get("project") or "run:" + run_id),
        )
    return decision


__all__ = ["observe_autopilot_task", "observe_codegen_build"]
