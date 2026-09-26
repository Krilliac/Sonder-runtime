"""E5: Workbench and Autopilot strategy attempts carry host-counted usage."""
from dataclasses import replace

from sonder_runtime.bootstrap.strategy import compose_strategy_trace
from sonder_runtime.bootstrap.strategy_observers import (
    observe_autopilot_task,
    observe_workbench_lane,
)
from sonder_runtime.domain.strategy.models import (
    ProgressMetric,
    ProgressVector,
    StrategyAction,
    StrategyBudget,
    StrategyUsage,
)


def _trace(tmp_path, name="strategy"):
    return compose_strategy_trace(
        db_path=tmp_path / f"{name}.db", key_path=tmp_path / "private" / f"{name}.key",
    )


def _lane(tmp_path, *, attempt_id="attempt-1", used_steps=3, status="completed",
          max_steps=8, **extra):
    return {
        "id": "lane-1", "attempt_id": attempt_id, "workspace_root": str(tmp_path),
        "status": status, "task": "implement parser", "tier": "code",
        "max_steps": max_steps, "used_steps": used_steps, **extra,
    }


def _reopen(tmp_path):
    return _trace(tmp_path)


def test_workbench_charges_each_attempt_its_own_model_turns(tmp_path):
    trace = _trace(tmp_path)
    observe_workbench_lane(trace, lane=_lane(tmp_path, used_steps=3))
    observe_workbench_lane(
        trace, lane=_lane(tmp_path, attempt_id="attempt-2", used_steps=5, status="failed",
                          error="LANE_ATTEMPT_FAILED"),
    )
    # A replay of the terminal attempt is idempotent and does not re-price it.
    observe_workbench_lane(
        trace, lane=_lane(tmp_path, attempt_id="attempt-2", used_steps=5, status="failed",
                          error="LANE_ATTEMPT_FAILED"),
    )

    history = _reopen(tmp_path).history("lane-1")
    assert [item.usage.model_calls for item in history] == [3, 2]
    assert all(item.usage.tool_calls == 0 for item in history)
    assert [item.model_route for item in history] == ["code", "code"]
    assert _reopen(tmp_path).sealed_budget("lane-1") == StrategyBudget(attempts=8, model_calls=8)


def test_workbench_ignores_non_integer_step_counters(tmp_path):
    trace = _trace(tmp_path)
    for index, bad in enumerate((None, "7", True, -4, 2.5)):
        observe_workbench_lane(
            trace, lane=dict(_lane(tmp_path, attempt_id=f"attempt-{index}"),
                             id=f"lane-{index}", used_steps=bad),
        )
        assert trace.history(f"lane-{index}")[0].usage == StrategyUsage(attempts=1)


def test_workbench_replays_attempts_sealed_before_attribution(tmp_path):
    scratch = _trace(tmp_path, "scratch")
    observe_workbench_lane(scratch, lane=_lane(tmp_path, used_steps=20, max_steps=32))
    legacy = replace(scratch.history("lane-1")[0], usage=StrategyUsage(attempts=1))
    trace = _trace(tmp_path)
    trace.record(legacy, budget=StrategyBudget(attempts=32),
                 available_actions=(StrategyAction.INSPECT,), transport_replay_safe=False)

    replayed = observe_workbench_lane(trace, lane=_lane(tmp_path, used_steps=20, max_steps=32))
    assert replayed is not None
    assert trace.history("lane-1") == (legacy,)

    # The next attempt is charged every uncharged lane turn, and its budget
    # never expands past the legacy seal (model_calls=12).
    decision = observe_workbench_lane(
        trace, lane=_lane(tmp_path, attempt_id="attempt-2", used_steps=24, max_steps=32),
    )
    history = _reopen(tmp_path).history("lane-1")
    assert history[-1].usage.model_calls == 24
    assert _reopen(tmp_path).sealed_budget("lane-1").model_calls == 12
    assert decision.action is StrategyAction.FAIL


def _run():
    return {"id": "run-1", "objective": "validate project", "project": "/work", "tier": "code"}


def _task(**extra):
    return {"id": "task-01", "kind": "validate", "attempts": 1, "status": "passed",
            "instruction": "run tests", **extra}


def _metrics(vector):
    return {item.name: item.value for item in vector.metrics}


def test_autopilot_records_host_receipt_counters_and_validation_progress(tmp_path):
    trace = _trace(tmp_path)
    observe_autopilot_task(trace, run=_run(), task=_task(host_receipt={
        "schema": 1, "tools": ["file_read", "shell"], "mutation_observed": False,
        "validation_attempted": True, "validation_passed": True,
    }))

    attempt = _reopen(tmp_path).history("run-1")[0]
    assert attempt.usage == StrategyUsage(attempts=1, tool_calls=2, verifier_calls=1)
    assert _metrics(attempt.progress_before) == {"task_passed": 0, "validation_passed": 0}
    assert _metrics(attempt.progress_after) == {"task_passed": 1, "validation_passed": 1}
    assert attempt.model_route == "code"


def test_autopilot_passed_task_without_passing_receipt_scores_no_validation(tmp_path):
    trace = _trace(tmp_path)
    observe_autopilot_task(trace, run=_run(), task=_task(host_receipt={
        "tools": ["shell"], "validation_attempted": True, "validation_passed": False,
    }))
    attempt = trace.history("run-1")[0]
    assert _metrics(attempt.progress_after) == {"task_passed": 1, "validation_passed": 0}


def test_autopilot_non_validate_task_keeps_single_progress_metric(tmp_path):
    trace = _trace(tmp_path)
    observe_autopilot_task(trace, run=_run(), task=_task(
        kind="inspect", host_receipt={"tools": ["file_read"], "validation_passed": True},
    ))
    attempt = trace.history("run-1")[0]
    assert _metrics(attempt.progress_after) == {"task_passed": 1}
    assert attempt.usage == StrategyUsage(attempts=1, tool_calls=1)


def test_autopilot_malformed_receipts_fall_back_to_one_attempt(tmp_path):
    trace = _trace(tmp_path)
    receipts = (
        None, ["file_read"], "validation_attempted", 7,
        {"tools": "file_read", "validation_attempted": "true", "validation_passed": 1},
        {"tools": {"file_read": 1}, "validation_attempted": 1},
    )
    for index, receipt in enumerate(receipts):
        run = dict(_run(), id=f"run-{index}")
        observe_autopilot_task(trace, run=run, task=_task(host_receipt=receipt))
        attempt = trace.history(run["id"])[0]
        assert attempt.usage == StrategyUsage(attempts=1)
        assert _metrics(attempt.progress_after)["validation_passed"] == 0


def test_autopilot_tool_count_is_clamped(tmp_path):
    trace = _trace(tmp_path)
    observe_autopilot_task(trace, run=_run(), task=_task(
        kind="inspect", host_receipt={"tools": [f"tool-{n}" for n in range(200)]},
    ))
    assert trace.history("run-1")[0].usage.tool_calls == 64


def test_autopilot_replays_attempts_sealed_before_attribution(tmp_path):
    trace = _trace(tmp_path)
    scratch = _trace(tmp_path, "scratch")
    receipt = {"tools": ["shell"], "validation_attempted": True, "validation_passed": True}
    observe_autopilot_task(scratch, run=_run(), task=_task(host_receipt=receipt))
    current = scratch.history("run-1")[0]
    scope = current.progress_before.scope_digest
    legacy = replace(
        current, usage=StrategyUsage(attempts=1),
        progress_before=ProgressVector(scope, (ProgressMetric("task_passed", 0, "maximize"),)),
        progress_after=ProgressVector(scope, (ProgressMetric("task_passed", 1, "maximize"),)),
    )
    trace.record(legacy, budget=StrategyBudget(attempts=50, model_calls=100,
                                               strategy_switches=50, replans=6),
                 available_actions=(StrategyAction.INSPECT, StrategyAction.REPLAN),
                 transport_replay_safe=False)

    assert observe_autopilot_task(trace, run=_run(), task=_task(host_receipt=receipt)) is not None
    assert trace.history("run-1") == (legacy,)
    assert trace.sealed_budget("run-1").tool_calls == 64


def test_autopilot_interrupted_retry_is_not_charged_the_previous_receipt(tmp_path):
    from autopilot_controller import _mark_interrupted_tasks_uncertain

    trace = _trace(tmp_path)
    receipt = {"tools": ["file_read", "shell", "file_write"],
               "validation_attempted": True, "validation_passed": False}
    task = _task(status="failed", host_receipt=receipt, error="tests failed")
    observe_autopilot_task(trace, run=_run(), task=task)

    # The controller's retry path: status and attempt number change, the
    # completed attempt's receipt stays on the task, then the controller dies.
    task = dict(task, status="running", attempts=2)
    plan = [task]
    assert _mark_interrupted_tasks_uncertain(plan) == 1
    decision = observe_autopilot_task(trace, run=_run(), task=plan[0])

    first, second = _reopen(tmp_path).history("run-1")
    assert first.usage == StrategyUsage(attempts=1, tool_calls=3, verifier_calls=1)
    assert second.attempt_id == "task-01-attempt-2"
    assert second.outcome == "uncertain"
    assert second.usage == StrategyUsage(attempts=1)
    assert _metrics(second.progress_after) == {"task_passed": 0, "validation_passed": 0}
    assert decision.action is StrategyAction.RECONCILE
