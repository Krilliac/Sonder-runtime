import os

import pytest

import autopilot_controller
import autopilot_store


@pytest.fixture(autouse=True)
def isolated_autopilot_db(monkeypatch, tmp_path):
    monkeypatch.setenv("TRILOBITE_AUTOPILOT_DB", str(tmp_path / "autopilot.db"))
    autopilot_store.reset_schema_cache_for_tests()
    yield
    autopilot_store.reset_schema_cache_for_tests()


def _plan(tasks=None):
    return {
        "summary": "grounded plan",
        "success_criteria": ["Requested result is inspected and validated"],
        "tasks": tasks or [
            {"title": "Inspect", "kind": "inspect", "instruction": "Inspect evidence"},
            {"title": "Validate", "kind": "validate", "instruction": "Run checks"},
        ],
    }


def _evidence(tool="file_read"):
    return (
        "Task completed.\n\n=== TOOL EVIDENCE ===\n"
        "step 1 tool=%s reason=ground the result\nPASS" % tool
    )


def _complete(_run, _issue):
    return {"decision": "complete", "reason": "criteria verified", "tasks": []}


def test_normalize_plan_injects_validation_and_deduplicates():
    normalized = autopilot_controller.normalize_plan(
        _plan([
            {"title": "Inspect", "kind": "inspect", "instruction": "Read it"},
            {"title": "Inspect", "kind": "inspect", "instruction": "Read it"},
        ]),
        "Inspect a project",
        4,
    )
    assert [task["kind"] for task in normalized["tasks"]] == ["inspect", "validate"]
    assert normalized["tasks"][1]["id"] == "task-02"


def test_successful_run_completes_only_after_validation_and_review():
    run = autopilot_store.create_run("Inspect and validate")
    result = autopilot_controller.execute_run(
        run["id"], "owner", owner_pid=os.getpid(),
        plan_fn=lambda _run: _plan(),
        work_fn=lambda _run, task, _prior: _evidence(
            "workspace_run" if task["kind"] == "validate" else "file_read"
        ),
        review_fn=_complete,
        max_cycles=2,
    )
    assert result["status"] == "completed"
    assert all(task["status"] == "passed" for task in result["plan"])
    assert "host-verified task evidence" in result["summary"]
    assert "action: workspace_run" in result["final_report"]


def test_missing_tool_evidence_fails_and_pauses_for_review():
    run = autopilot_store.create_run("Ground every claim")
    result = autopilot_controller.execute_run(
        run["id"], "owner", owner_pid=os.getpid(),
        plan_fn=lambda _run: _plan(),
        work_fn=lambda *_args: "unsupported prose-only claim",
        review_fn=lambda _run, _issue: {
            "decision": "pause", "reason": "needs grounded evidence", "tasks": [],
        },
    )
    assert result["status"] == "paused"
    assert result["failures"] == 1
    assert result["plan"][0]["status"] == "failed"
    assert "no host-observed tool evidence" in result["plan"][0]["error"]


def test_failed_task_can_retry_once_with_reviewer_instruction():
    run = autopilot_store.create_run("Retry carefully")
    calls = {"work": 0}

    def work(_run, task, _prior):
        calls["work"] += 1
        if calls["work"] == 1:
            return "ERROR: transient failure"
        return _evidence("workspace_run" if task["kind"] == "validate" else "file_read")

    def review(current, issue):
        if issue != "host completion gates passed":
            return {
                "decision": "retry", "reason": "correct the exact failure",
                "instruction": "retry with inspected evidence", "tasks": [],
            }
        return _complete(current, issue)

    result = autopilot_controller.execute_run(
        run["id"], "owner", owner_pid=os.getpid(),
        plan_fn=lambda _run: _plan(), work_fn=work, review_fn=review,
    )
    assert result["status"] == "completed"
    assert result["failures"] == 1
    assert result["plan"][0]["attempts"] == 2


def test_replan_respects_one_remaining_slot_and_preserves_validation():
    run = {
        "objective": "finish safely", "max_tasks": 3,
        "criteria": ["validated"],
        "plan": [
            {"id": "task-01", "title": "Failed", "instruction": "old", "kind": "implement", "status": "failed", "history": []},
            {"id": "task-02", "title": "Validate", "instruction": "check", "kind": "validate", "status": "pending", "history": []},
        ],
    }
    plan = autopilot_controller._append_replan(
        run, 0,
        [{"title": "Replacement", "kind": "implement", "instruction": "new"}],
    )
    assert len(plan) == 3
    assert plan[0]["status"] == "superseded"
    assert plan[-1]["title"] == "Replacement"
    assert any(task["kind"] == "validate" for task in plan)


def test_plan_only_persists_ready_plan_without_execution():
    run = autopilot_store.create_run("Plan this")
    result = autopilot_controller.execute_run(
        run["id"], "owner", owner_pid=os.getpid(),
        plan_fn=lambda _run: _plan(),
        work_fn=lambda *_args: pytest.fail("work must not run"),
        review_fn=_complete,
        plan_only=True,
    )
    assert result["status"] == "paused"
    assert result["cycles"] == 0
    assert len(result["plan"]) == 2


def test_pause_and_cancel_are_checked_between_tasks():
    paused_run = autopilot_store.create_run("pause at checkpoint")

    def pause_work(run, _task, _prior):
        autopilot_store.request_pause(run["id"])
        return _evidence()

    paused = autopilot_controller.execute_run(
        paused_run["id"], "pause-owner", owner_pid=os.getpid(),
        plan_fn=lambda _run: _plan(), work_fn=pause_work, review_fn=_complete,
    )
    assert paused["status"] == "paused"
    assert paused["cycles"] == 1

    cancelled_run = autopilot_store.create_run("cancel active result")

    def cancel_work(run, _task, _prior):
        autopilot_store.request_cancel(run["id"])
        return _evidence()

    cancelled = autopilot_controller.execute_run(
        cancelled_run["id"], "cancel-owner", owner_pid=os.getpid(),
        plan_fn=lambda _run: _plan(), work_fn=cancel_work, review_fn=_complete,
    )
    assert cancelled["status"] == "cancelled"
    assert cancelled["cycles"] == 0


def test_per_invocation_cycle_budget_pauses_with_progress():
    run = autopilot_store.create_run("bounded progress")
    result = autopilot_controller.execute_run(
        run["id"], "owner", owner_pid=os.getpid(),
        plan_fn=lambda _run: _plan(), work_fn=lambda *_args: _evidence(),
        review_fn=_complete, max_cycles=1,
    )
    assert result["status"] == "paused"
    assert result["cycles"] == 1
    assert "cycle budget" in result["summary"]
