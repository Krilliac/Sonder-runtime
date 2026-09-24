"""Autopilot consumes sealed strategy references before its actual model turn."""

import os
import sqlite3
from types import SimpleNamespace

import pytest

import autopilot_controller
import server
from sonder_runtime.adapters.persistence import autopilot_store
from sonder_runtime.adapters.unit_of_work import UnitOfWorkAdapter
from sonder_runtime.application.memory.strategy_memory import StrategyMemoryService
from sonder_runtime.bootstrap.strategy import compose_strategy_trace
from sonder_runtime.bootstrap.strategy_observers import observe_autopilot_task


@pytest.fixture(autouse=True)
def autopilot_database(monkeypatch, tmp_path):
    monkeypatch.setenv("SONDER_AUTOPILOT_DB", str(tmp_path / "autopilot.db"))
    autopilot_store.reset_schema_cache_for_tests()
    yield
    autopilot_store.reset_schema_cache_for_tests()


def _memory(tmp_path):
    trace = compose_strategy_trace(
        db_path=tmp_path / "checkpoint.db",
        key_path=tmp_path / "private" / "checkpoint.key",
    )
    database = tmp_path / "memory.db"
    memory = StrategyMemoryService(trace, lambda: UnitOfWorkAdapter(str(database)))
    return trace, memory, database


def _seed_failed_inspection(trace, memory, project):
    observe_autopilot_task(
        trace,
        run={"id": "prior-autopilot", "objective": "inspect project", "project": project,
             "tier": "code"},
        task={"id": "task-01", "kind": "inspect", "attempts": 1,
              "status": "failed", "error": "missing dependency"},
        memory_service=memory,
    )


@pytest.mark.parametrize("fail_after_response", [False, True])
def test_live_task_reads_sealed_prior_reference_before_model_call(
    monkeypatch, tmp_path, fail_after_response,
):
    project = tmp_path / "project"
    project.mkdir()
    trace, memory, database = _memory(tmp_path)
    _seed_failed_inspection(trace, memory, str(project))
    run = autopilot_store.create_run(
        "Inspect and validate", project=str(project), adaptive=False,
    )
    seen = []
    replies = iter(
        ('{"final":"Inspected without host tool evidence"}',)
        if fail_after_response else (
            '{"tool":"file_read","args":{"path":"manifest.txt"}}',
            '{"final":"Inspected manifest"}',
        )
    )

    def generate(_model, _system, _temperature, _predict, window, **_kwargs):
        assert window == 32768  # The context plan and dispatch pin one window.

        def call(prompt, history=None):
            # No attempt is sealed yet, and the durable selection preceded
            # the first provider call. The model sees only typed references.
            if not seen:
                with sqlite3.connect(database) as connection:
                    selections = connection.execute(
                        "SELECT experience_id, outcome FROM strategy_memory_selection"
                    ).fetchall()
                seen.append((prompt, selections, trace.history(run["id"])))
            return next(replies, '{"final":"Inspected manifest"}')

        return call

    monkeypatch.setenv("SONDER_SPECULATION", "0")
    monkeypatch.setattr(server, "_serve_target", lambda tier, strict: ("measured-model", False, False, "code"))
    monkeypatch.setattr(server, "_auto_model_context", lambda model: 32768)
    monkeypatch.setattr(server, "_build_system", lambda *args, **kwargs: "host instruction")
    monkeypatch.setattr(server, "_make_generate", generate)
    monkeypatch.setattr(server, "_agent_dispatch_observed", lambda *args, **kwargs: "file inspected")

    result = autopilot_controller.execute_run(
        run["id"], "owner", owner_pid=os.getpid(),
        plan_fn=lambda _run: {
            "summary": "bounded plan", "success_criteria": ["inspect and validate"],
            "tasks": [
                {"title": "Inspect", "kind": "inspect", "instruction": "Inspect project"},
                {"title": "Validate", "kind": "validate", "instruction": "Validate project"},
            ],
        },
        work_fn=lambda current, task, prior: server._autopilot_work_model(
            current, task, prior, strategy_memory=memory,
        ),
        review_fn=lambda *_: {"decision": "pause", "reason": "one task only"},
        max_cycles=1, strategy_trace=trace, strategy_memory=memory,
    )

    assert len(seen) == 1
    prompt, selections, history = seen[0]
    assert len(selections) == 1 and selections[0][1] is None and history == ()
    assert selections[0][0] in prompt
    assert '"authority":"advisory_only"' in prompt
    assert '"failure":"dependency_failure"' in prompt
    assert "missing dependency" not in prompt
    assert trace.history(run["id"]), {key: result.get(key) for key in
                                       ("status", "summary", "last_error", "plan")}
    observed = trace.history(run["id"])[0]
    assert observed.attempt_id == "task-01-attempt-1"
    with sqlite3.connect(database) as connection:
        selected_outcome = connection.execute(
            "SELECT outcome FROM strategy_memory_selection"
        ).fetchone()[0]
    assert selected_outcome == observed.outcome
    assert result["plan"][0]["host_receipt"]["pre_model_context_response_observed"] is True
    if fail_after_response:
        assert selected_outcome == "failed"
        with memory._unit_of_work() as scope:
            assert scope.strategy_experiences.failed_reuses(selections[0][0]) == 1


def test_unmeasured_or_full_window_does_not_claim_exposure(monkeypatch, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    trace, memory, database = _memory(tmp_path)
    _seed_failed_inspection(trace, memory, str(project))
    seen = []

    def agent(prompt, **kwargs):
        callback = kwargs["pre_model_context"]
        assert callback is not None
        seen.append(callback("hosted-model", True, "system", prompt, 1000, 0))
        seen.append(callback("tiny-model", False, "system", prompt, 1200, 1500))
        return "paused"

    monkeypatch.setattr(server, "_agent_impl", agent)
    monkeypatch.setattr(server, "_autopilot_allowed_tools", lambda run: frozenset({"file_read"}))
    monkeypatch.setattr(server, "_autopilot_tool_policy", lambda run: None)
    run = {"id": "new-autopilot", "project": str(project), "owner_id": "owner",
           "tier": "code", "policy": "observe"}
    task = {"id": "task-01", "kind": "inspect", "title": "Inspect",
            "instruction": "Inspect project", "attempts": 1}
    assert server._autopilot_work_model(run, task, "", strategy_memory=memory) == "paused"
    assert seen == ["", ""]
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM strategy_memory_selection",
        ).fetchone()[0] == 0


def test_unanswered_model_request_does_not_penalize_selected_reference(monkeypatch, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    trace, memory, database = _memory(tmp_path)
    _seed_failed_inspection(trace, memory, str(project))
    run = autopilot_store.create_run(
        "Inspect and validate", project=str(project), adaptive=False,
    )
    model_calls = []

    def generate(_model, _system, _temperature, _predict, window, **_kwargs):
        assert window == 32768

        def call(prompt, history=None):
            model_calls.append(prompt)
            # A local transport refusal returns without a model response.
            raise server.ModelCallError("connection", "local model unavailable", attempts=0)

        return call

    monkeypatch.setenv("SONDER_SPECULATION", "0")
    monkeypatch.setattr(server, "_serve_target", lambda tier, strict: ("measured-model", False, False, "code"))
    monkeypatch.setattr(server, "_auto_model_context", lambda model: 32768)
    monkeypatch.setattr(server, "_build_system", lambda *args, **kwargs: "host instruction")
    monkeypatch.setattr(server, "_make_generate", generate)
    result = autopilot_controller.execute_run(
        run["id"], "owner", owner_pid=os.getpid(),
        plan_fn=lambda _run: {
            "summary": "bounded plan", "success_criteria": ["inspect and validate"],
            "tasks": [
                {"title": "Inspect", "kind": "inspect", "instruction": "Inspect project"},
                {"title": "Validate", "kind": "validate", "instruction": "Validate project"},
            ],
        },
        work_fn=lambda current, task, prior: server._autopilot_work_model(
            current, task, prior, strategy_memory=memory,
        ),
        review_fn=lambda *_: {"decision": "pause", "reason": "one task only"},
        max_cycles=1, strategy_trace=trace, strategy_memory=memory,
    )
    assert len(model_calls) == 1
    assert trace.history(run["id"])[0].outcome == "failed"
    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            "SELECT experience_id, outcome FROM strategy_memory_selection"
        ).fetchall()
    assert len(rows) == 1 and rows[0][0] in model_calls[0]
    assert rows[0][1] is None, result.get("status")
    assert result["plan"][0]["host_receipt"].get("pre_model_context_response_observed") is None
    with memory._unit_of_work() as scope:
        assert scope.strategy_experiences.failed_reuses(rows[0][0]) == 0
    # Restart/re-observation must preserve the host receipt's missing-response
    # evidence rather than backfilling a failed reuse from the task verdict.
    observe_autopilot_task(
        trace, run=result, task=result["plan"][0], memory_service=memory,
    )
    with memory._unit_of_work() as scope:
        assert scope.strategy_experiences.failed_reuses(rows[0][0]) == 0


def test_autopilot_entry_wires_host_memory_to_task_worker(monkeypatch, tmp_path):
    monkeypatch.setenv("SONDER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SONDER_STRATEGY_OBSERVE", "1")
    monkeypatch.setattr(server, "_application", lambda: SimpleNamespace(
        unit_of_work=lambda: UnitOfWorkAdapter(str(tmp_path / "memory.db")),
    ))
    seen = []

    def worker(run, task, prior, *, strategy_memory=None):
        seen.append((run["id"], strategy_memory, task["id"], prior))
        return "ready"

    def execute(run_id, owner_id, **options):
        assert options["strategy_memory"] is not None
        assert options["strategy_trace"] is not None
        assert options["work_fn"]({"id": run_id}, {"id": "task-01"}, "prior") == "ready"
        return {"id": run_id, "status": "paused"}

    monkeypatch.setattr(server, "_autopilot_work_model", worker)
    monkeypatch.setattr(server.autopilot_controller, "execute_run", execute)
    assert server._execute_autopilot("durable-run", plan_only=True)["status"] == "paused"
    assert seen[0][0] == "durable-run" and seen[0][1] is not None
    assert seen[0][2:] == ("task-01", "prior")
