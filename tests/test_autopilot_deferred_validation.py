"""Controller-level policy tests for non-executing and deferred validation."""

import os

import autopilot_controller
import sonder_runtime.adapters.persistence.autopilot_store as autopilot_store


def _plan():
    return {
        "summary": "write and validate an artifact",
        "success_criteria": ["the artifact is present and checked"],
        "tasks": [
            {"title": "Write artifact", "kind": "implement", "instruction": "write it"},
            {"title": "Run validator", "kind": "validate", "instruction": "run the exact check"},
            {"title": "Report result", "kind": "report", "instruction": "report the evidence"},
        ],
    }


def _review(_run, _reason):
    return {"decision": "complete", "reason": "report the evidence", "tasks": []}


def _receipt(*, deferred=False, required="", validation=False, mutation=False):
    return autopilot_controller.HostTaskResult(
        output=(
            "written, not executed: needs " + required + " to verify\n"
            "Task completed."
            if deferred
            else "Task completed."
        ),
        tools=("file_write",) if mutation else ("permission_preflight",),
        mutation_observed=mutation,
        validation_attempted=validation,
        validation_passed=validation,
        validation_evidence=(
            {"path": "index.html", "status": "passed", "readback_tool": "file_read"},
        ) if mutation and validation else (),
        verification_deferred=deferred,
        verification_required=required,
    )


def test_static_evidence_is_accepted_for_an_implementation():
    result = _receipt(mutation=True, validation=True)

    passed, error = autopilot_controller._task_passed(result, {"kind": "implement"})

    assert passed is True
    assert error == ""
    assert result.receipt()["validation_evidence"][0]["readback_tool"] == "file_read"


def test_receipt_additions_preserve_original_positional_constructor():
    result = autopilot_controller.HostTaskResult("done", ("file_read",), False, True, True, "selected-root")
    assert result.project_scope == "selected-root"
    assert result.validation_evidence == ()
    assert result.receipt() == {
        "schema": 1, "tools": ["file_read"], "mutation_observed": False,
        "validation_attempted": True, "validation_passed": True,
    }


def test_refused_implementation_is_passed_unverified_and_reported(tmp_path, monkeypatch):
    monkeypatch.setenv("SONDER_AUTOPILOT_DB", str(tmp_path / "autopilot.db"))
    autopilot_store.reset_schema_cache_for_tests()
    run = autopilot_store.create_run("write an artifact")

    def work(_run, task, _prior):
        if task["kind"] == "implement":
            return _receipt(deferred=True, required="workspace_run", mutation=True)
        if task["kind"] == "validate":
            return _receipt(deferred=True, required="workspace_run")
        return _receipt()

    result = autopilot_controller.execute_run(
        run["id"], "owner", owner_pid=os.getpid(), plan_fn=lambda _run: _plan(),
        work_fn=work, review_fn=_review, max_cycles=12,
    )

    assert result["status"] == "paused"
    assert result["failures"] == 0
    assert result["plan"][0]["status"] == "passed_unverified"
    assert result["plan"][1]["status"] == "pending"
    assert result["plan"][2]["status"] == "passed"
    assert "written, not executed: needs workspace_run to verify" in result["plan"][0]["error"]
    assert "needs approval to run workspace_run" in result["plan"][1]["error"]
    assert "written, not executed: needs workspace_run to verify" in result["final_report"]


def test_deferred_failed_implementation_still_uses_failure_review(tmp_path, monkeypatch):
    monkeypatch.setenv("SONDER_AUTOPILOT_DB", str(tmp_path / "autopilot.db"))
    autopilot_store.reset_schema_cache_for_tests()
    run = autopilot_store.create_run("reject an invalid artifact")
    result = autopilot_controller.execute_run(
        run["id"], "owner", owner_pid=os.getpid(), plan_fn=lambda _run: _plan(),
        work_fn=lambda *_args: autopilot_controller.HostTaskResult(
            output="VALIDATION_FAILED: malformed artifact",
            tools=("file_write",), mutation_observed=True,
            verification_deferred=True, verification_required="test_run",
        ),
        review_fn=lambda _run, issue: {
            "decision": "pause", "reason": issue, "tasks": [],
        },
        max_cycles=12,
    )
    assert result["status"] == "paused"
    assert result["plan"][0]["status"] == "failed"
    assert result["failures"] == 1


def test_deferred_validate_does_not_consume_failure_budget(tmp_path, monkeypatch):
    monkeypatch.setenv("SONDER_AUTOPILOT_DB", str(tmp_path / "autopilot.db"))
    autopilot_store.reset_schema_cache_for_tests()
    run = autopilot_store.create_run("validate an artifact")

    result = autopilot_controller.execute_run(
        run["id"], "owner", owner_pid=os.getpid(),
        plan_fn=lambda _run: {
            "summary": "validate",
            "success_criteria": ["checked"],
            "tasks": [{"title": "Validate", "kind": "validate", "instruction": "run it"}],
        },
        work_fn=lambda *_args: _receipt(deferred=True, required="test_run"),
        review_fn=_review,
        max_cycles=1,
    )

    assert result["status"] == "paused"
    assert result["failures"] == 0
    assert result["plan"][0]["attempts"] == 1
    assert result["plan"][0]["status"] == "pending"
    assert result["plan"][0]["verification_deferred"] is True
    assert "needs approval to run test_run" in result["plan"][0]["error"]


def test_resume_clears_deferred_fence_and_retries_validator(tmp_path, monkeypatch):
    monkeypatch.setenv("SONDER_AUTOPILOT_DB", str(tmp_path / "autopilot.db"))
    autopilot_store.reset_schema_cache_for_tests()
    run = autopilot_store.create_run("validate an artifact")
    calls = []

    def work(_run, task, _prior):
        calls.append(task["kind"])
        if len(calls) == 1:
            return _receipt(deferred=True, required="test_run")
        return _receipt(validation=True)

    first = autopilot_controller.execute_run(
        run["id"], "owner", owner_pid=os.getpid(),
        plan_fn=lambda _run: {
            "summary": "validate",
            "success_criteria": ["checked"],
            "tasks": [{"title": "Validate", "kind": "validate", "instruction": "run it"}],
        },
        work_fn=work, review_fn=_review, max_cycles=1,
    )
    assert first["plan"][0]["verification_deferred"] is True

    second = autopilot_controller.execute_run(
        run["id"], "owner-2", owner_pid=os.getpid(),
        plan_fn=lambda _run: (_ for _ in ()).throw(AssertionError("must retain plan")),
        work_fn=work, review_fn=_review, max_cycles=1,
    )

    assert calls == ["validate", "validate"]
    assert second["plan"][0]["status"] == "passed"
    assert second["plan"][0].get("verification_deferred") is None
