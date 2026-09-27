"""Autopilot wall-clock budget and cancelled-task status (finding 41).

* ``max_cycles`` bounded only the number of tasks; each task is a full
  bounded agent run, so an invocation had no wall-time bound.  A
  per-invocation wall-clock budget now pauses the run at the next host
  checkpoint once it is spent.
* A cancelled run kept its in-flight task shown as ``[running]`` (and later
  tasks as ``[pending]``) forever.  Cancel now closes them as ``cancelled``.
"""
import os

import pytest

import autopilot_controller
import sonder_runtime.adapters.persistence.autopilot_store as autopilot_store


@pytest.fixture(autouse=True)
def isolated_autopilot_db(monkeypatch, tmp_path):
    monkeypatch.setenv("SONDER_AUTOPILOT_DB", str(tmp_path / "autopilot.db"))
    monkeypatch.delenv("SONDER_AUTOPILOT_MAX_WALL_SECONDS", raising=False)
    autopilot_store.reset_schema_cache_for_tests()
    yield
    autopilot_store.reset_schema_cache_for_tests()


def _plan():
    return {
        "summary": "grounded plan",
        "success_criteria": ["Requested result is inspected and validated"],
        "tasks": [
            {"title": "Inspect", "kind": "inspect", "instruction": "Inspect evidence"},
            {"title": "Inspect more", "kind": "inspect", "instruction": "Inspect more"},
            {"title": "Validate", "kind": "validate", "instruction": "Run checks"},
        ],
    }


def _evidence():
    return autopilot_controller.HostTaskResult(
        output="Task completed.\n\n=== TOOL EVIDENCE ===\nstep 1 tool=file_read reason=x\nPASS",
        tools=("file_read",),
        mutation_observed=False,
        validation_attempted=False,
        validation_passed=False,
    )


def _continue(_run, _reason):
    return {"decision": "continue", "reason": "keep going", "tasks": []}


def test_wall_clock_budget_pauses_the_run_at_the_next_checkpoint(monkeypatch):
    clock = {"now": 1000.0}
    monkeypatch.setattr(autopilot_controller, "_monotonic", lambda: clock["now"])
    run = autopilot_store.create_run("slow tasks", adaptive=False)
    worked = []

    def slow_work(_run, task, _prior):
        worked.append(task["id"])
        clock["now"] += 40.0  # each task takes 40s of wall time
        return _evidence()

    result = autopilot_controller.execute_run(
        run["id"], "owner", owner_pid=os.getpid(),
        plan_fn=lambda _run: _plan(), work_fn=slow_work, review_fn=_continue,
        max_cycles=12, max_wall_seconds_budget=60,
    )

    assert result["status"] == "paused"
    assert "wall-clock budget" in result["summary"]
    # Task 1 ends at 40s (< 60), task 2 ends at 80s; no third task starts.
    assert len(worked) == 2
    assert [task["status"] for task in result["plan"]] == ["passed", "passed", "pending"]


def test_wall_budget_is_configurable_and_bounded(monkeypatch):
    assert autopilot_controller.max_wall_seconds() == 3600
    monkeypatch.setenv("SONDER_AUTOPILOT_MAX_WALL_SECONDS", "90")
    assert autopilot_controller.max_wall_seconds() == 90
    monkeypatch.setenv("SONDER_AUTOPILOT_MAX_WALL_SECONDS", "nonsense")
    assert autopilot_controller.max_wall_seconds() == 3600
    monkeypatch.setenv("SONDER_AUTOPILOT_MAX_WALL_SECONDS", "-5")
    assert autopilot_controller.max_wall_seconds() == 3600
    assert autopilot_controller.max_wall_seconds(10 ** 9) == 24 * 60 * 60
    assert autopilot_controller.max_wall_seconds(30) == 30


def test_cancel_during_a_task_marks_open_tasks_cancelled():
    run = autopilot_store.create_run("cancel active result")

    def cancel_work(current, _task, _prior):
        autopilot_store.request_cancel(current["id"])
        return _evidence()

    result = autopilot_controller.execute_run(
        run["id"], "owner", owner_pid=os.getpid(),
        plan_fn=lambda _run: _plan(), work_fn=cancel_work, review_fn=_continue,
    )

    assert result["status"] == "cancelled"
    statuses = [task["status"] for task in result["plan"]]
    assert statuses == ["cancelled", "cancelled", "cancelled"]
    assert "running" not in autopilot_controller.format_run(result)
    assert "result was discarded" in result["plan"][0]["error"]
    assert result["plan"][1]["error"] == "cancelled before it started"


def test_cancel_of_a_paused_run_closes_pending_but_keeps_evidence():
    run = autopilot_store.create_run("paused then cancelled", adaptive=False)
    paused = autopilot_controller.execute_run(
        run["id"], "owner", owner_pid=os.getpid(),
        plan_fn=lambda _run: _plan(), work_fn=lambda *_a: _evidence(),
        review_fn=_continue, max_cycles=1,
    )
    assert paused["status"] == "paused"

    cancelled = autopilot_store.request_cancel(run["id"])

    assert cancelled["status"] == "cancelled"
    assert [task["status"] for task in cancelled["plan"]] == [
        "passed", "cancelled", "cancelled",
    ]


def test_cancel_leaves_uncertain_tasks_uncertain():
    rewritten = autopilot_store._cancelled_plan_json(
        '[{"id": "t1", "status": "uncertain"}, {"id": "t2", "status": "failed"}]'
    )
    assert rewritten is None
