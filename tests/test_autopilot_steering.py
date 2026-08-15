"""Owner-scoped steering of active autopilot runs.

A run's owner may pause the run, ask it to stop for clarification, or attach a
bounded note the worker consumes at a safe host checkpoint.  The slice must
fail closed for legacy/unowned runs, refuse cross-account control, keep the
lease/cancel/no-auto-replay fences intact, and deliver the user text to the
model only as fenced untrusted data, never as policy or tool authority.
"""
import os

import pytest

import autopilot_controller
import autopilot_store


@pytest.fixture(autouse=True)
def isolated_autopilot_db(monkeypatch, tmp_path):
    path = tmp_path / "autopilot.db"
    monkeypatch.setenv("SONDER_AUTOPILOT_DB", str(path))
    autopilot_store.reset_schema_cache_for_tests()
    yield path
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


def _task_evidence(task):
    kind = task["kind"]
    tool = (
        "workspace_run" if kind == "validate"
        else "file_write" if kind == "implement" else "file_read"
    )
    output = (
        "Task completed.\n\n=== TOOL EVIDENCE ===\n"
        "step 1 tool=%s reason=ground the result\nPASS" % tool
    )
    return autopilot_controller.HostTaskResult(
        output=output,
        tools=(tool,),
        mutation_observed=kind == "implement",
        validation_attempted=kind == "validate",
        validation_passed=kind == "validate",
    )


def _complete(_run, _issue):
    return {"decision": "complete", "reason": "criteria verified", "tasks": []}


# --- ownership -------------------------------------------------------------

def test_attach_steering_is_strictly_owner_scoped():
    run = autopilot_store.create_run("account goal", request_owner="account-one")

    assert autopilot_store.attach_steering(
        run["id"], "wrong account", request_owner="account-two",
    ) is None
    note = autopilot_store.attach_steering(
        run["id"], "prefer the docs directory", request_owner="account-one",
    )
    assert note is not None
    assert note["run_id"] == run["id"]
    assert note["kind"] == "guidance"


def test_attach_steering_fails_closed_for_legacy_and_unowned_runs():
    legacy = autopilot_store.create_run("legacy unowned goal")
    assert legacy["request_owner"] == ""

    # A missing, empty, or None owner scope must never control a run, and an
    # unowned/legacy run must be unsteerable even by an "empty" scope match.
    assert autopilot_store.attach_steering(legacy["id"], "hi", request_owner=None) is None
    assert autopilot_store.attach_steering(legacy["id"], "hi", request_owner="") is None
    assert autopilot_store.attach_steering(
        legacy["id"], "hi", request_owner="account-one",
    ) is None


def test_attach_steering_refuses_terminal_runs():
    run = autopilot_store.create_run("cancel me", request_owner="account-one")
    autopilot_store.request_cancel(run["id"], request_owner="account-one")
    assert autopilot_store.get_run(run["id"])["status"] == "cancelled"
    assert autopilot_store.attach_steering(
        run["id"], "too late", request_owner="account-one",
    ) is None


# --- bounds ----------------------------------------------------------------

def test_steering_text_and_backlog_are_bounded():
    run = autopilot_store.create_run("bounded", request_owner="account-one")

    with pytest.raises(ValueError):
        autopilot_store.attach_steering(run["id"], "   ", request_owner="account-one")
    with pytest.raises(ValueError):
        autopilot_store.attach_steering(
            run["id"], "note", kind="policy_override", request_owner="account-one",
        )

    long_note = autopilot_store.attach_steering(
        run["id"], "x" * (autopilot_store.MAX_STEERING_CHARS + 500),
        request_owner="account-one",
    )
    assert len(long_note["message"]) == autopilot_store.MAX_STEERING_CHARS

    for index in range(autopilot_store.MAX_PENDING_STEERING - 1):
        assert autopilot_store.attach_steering(
            run["id"], "note %d" % index, request_owner="account-one",
        ) is not None
    assert autopilot_store.attach_steering(
        run["id"], "backlog is full", request_owner="account-one",
    ) is None


# --- worker fences ---------------------------------------------------------

def test_clarify_requests_cooperative_pause_on_active_run():
    run = autopilot_store.create_run("clarify me", request_owner="account-one")
    autopilot_store.claim_run(
        run["id"], "owner-a", owner_pid=os.getpid(), request_owner="account-one",
    )

    autopilot_store.attach_steering(
        run["id"], "which environment did you mean?", kind="clarify",
        request_owner="account-one",
    )
    stored = autopilot_store.get_run(run["id"])
    assert stored["pause_requested"] is True

    # Plain guidance never interrupts the run.
    fresh = autopilot_store.create_run("guide me", request_owner="account-one")
    autopilot_store.claim_run(
        fresh["id"], "owner-a", owner_pid=os.getpid(), request_owner="account-one",
    )
    autopilot_store.attach_steering(
        fresh["id"], "prefer smaller diffs", request_owner="account-one",
    )
    assert autopilot_store.get_run(fresh["id"])["pause_requested"] is False


def test_stale_worker_cannot_read_or_consume_steering():
    run = autopilot_store.create_run("stale worker", request_owner="account-one")
    autopilot_store.claim_run(
        run["id"], "owner-a", owner_pid=os.getpid(), request_owner="account-one",
    )
    note = autopilot_store.attach_steering(
        run["id"], "for the live owner only", request_owner="account-one",
    )

    assert autopilot_store.pending_steering(run["id"], "owner-b") == []
    assert autopilot_store.consume_steering(run["id"], "owner-b", [note["note_id"]]) == 0

    pending = autopilot_store.pending_steering(run["id"], "owner-a")
    assert [item["note_id"] for item in pending] == [note["note_id"]]

    # After the run is released the former owner is stale as well.
    autopilot_store.finish_run(run["id"], "owner-a", "paused", summary="released")
    assert autopilot_store.pending_steering(run["id"], "owner-a") == []
    assert autopilot_store.consume_steering(run["id"], "owner-a", [note["note_id"]]) == 0


# --- persistence / restart -------------------------------------------------

def test_steering_survives_process_restart():
    run = autopilot_store.create_run("durable steering", request_owner="account-one")
    note = autopilot_store.attach_steering(
        run["id"], "survive the restart", request_owner="account-one",
    )

    # A restarted controller process re-runs the idempotent schema bootstrap.
    autopilot_store.reset_schema_cache_for_tests()

    autopilot_store.claim_run(
        run["id"], "owner-after-restart", owner_pid=os.getpid(),
        request_owner="account-one",
    )
    pending = autopilot_store.pending_steering(run["id"], "owner-after-restart")
    assert [item["message"] for item in pending] == ["survive the restart"]
    assert autopilot_store.consume_steering(
        run["id"], "owner-after-restart", [note["note_id"]],
    ) == 1
    # Consumption is durable and one-shot: never redelivered after restart.
    autopilot_store.reset_schema_cache_for_tests()
    assert autopilot_store.pending_steering(run["id"], "owner-after-restart") == []


# --- controller delivery ---------------------------------------------------

def test_worker_consumes_steering_as_fenced_untrusted_context():
    run = autopilot_store.create_run("steered goal", request_owner="account-one")
    autopilot_store.attach_steering(
        run["id"], "please prioritise the docs folder", request_owner="account-one",
    )
    priors = []

    def work(_run, task, prior):
        priors.append(prior)
        return _task_evidence(task)

    result = autopilot_controller.execute_run(
        run["id"], "owner", owner_pid=os.getpid(), request_owner="account-one",
        plan_fn=lambda _run: _plan(), work_fn=work, review_fn=_complete,
    )
    assert result["status"] == "completed"
    steered = [text for text in priors if "please prioritise the docs folder" in text]
    assert steered, "steering note never reached the worker"
    assert "OPERATOR STEERING" in steered[0]
    assert "untrusted" in steered[0]
    assert "never" in steered[0] and "policy" in steered[0]
    # Consumed exactly once, durably.
    assert all(
        "please prioritise the docs folder" not in text
        for text in priors[priors.index(steered[0]) + 1:]
    )
    assert any(
        event["kind"] == "steer_consumed"
        for event in autopilot_store.events(run["id"], limit=50)
    )


def test_final_task_steering_reaches_completion_review_before_completion():
    run = autopilot_store.create_run("last task steering", request_owner="account-one")
    review_contexts = []

    def work(current, task, _prior):
        assert autopilot_store.attach_steering(
            current["id"], "include the final audit note", request_owner="account-one",
        ) is not None
        return _task_evidence(task)

    def review(_run, context):
        review_contexts.append(context)
        return _complete(_run, context)

    result = autopilot_controller.execute_run(
        run["id"], "owner", owner_pid=os.getpid(), request_owner="account-one",
        plan_fn=lambda _run: _plan([
            {"title": "Validate", "kind": "validate", "instruction": "Run checks"},
        ]),
        work_fn=work, review_fn=review,
    )

    assert result["status"] == "completed"
    assert len(review_contexts) == 1
    assert "include the final audit note" in review_contexts[0]
    assert "OPERATOR STEERING" in review_contexts[0]
    assert autopilot_store.pending_steering(run["id"], "owner") == []


def test_clarify_pauses_then_resume_delivers_the_answer():
    run = autopilot_store.create_run("pause for clarity", request_owner="account-one")
    priors = []

    def work(current, task, prior):
        priors.append(prior)
        if len(priors) == 1:
            # The owner interrupts mid-run with a clarification request.
            assert autopilot_store.attach_steering(
                current["id"], "stop: target staging, not production",
                kind="clarify", request_owner="account-one",
            ) is not None
        return _task_evidence(task)

    first = autopilot_controller.execute_run(
        run["id"], "owner", owner_pid=os.getpid(), request_owner="account-one",
        plan_fn=lambda _run: _plan(), work_fn=work, review_fn=_complete,
    )
    assert first["status"] == "paused"
    # The note is not consumed while the run sits paused.
    stored = autopilot_store.get_run(run["id"], request_owner="account-one")
    assert stored["owner_id"] == ""

    second = autopilot_controller.execute_run(
        run["id"], "owner", owner_pid=os.getpid(), request_owner="account-one",
        plan_fn=lambda _run: _plan(), work_fn=work, review_fn=_complete,
    )
    assert second["status"] == "completed"
    assert any(
        "stop: target staging, not production" in text for text in priors[1:]
    )


# --- server command surface ------------------------------------------------

def test_server_steer_command_fails_closed_without_account_scope():
    import server

    run = autopilot_store.create_run("served goal", request_owner="account-one")
    reply = server._autopilot_command(
        "steer %s focus on docs" % run["id"], request_owner=None,
    )
    assert "rejected" in reply
    assert run["id"] not in reply  # no existence leak to unscoped callers

    cross = server._autopilot_command(
        "steer %s focus on docs" % run["id"], request_owner="account-two",
    )
    assert "no accessible run" in cross

    accepted = server._autopilot_command(
        "steer %s focus on docs" % run["id"], request_owner="account-one",
    )
    assert "steering attached" in accepted

    clarified = server._autopilot_command(
        "clarify %s which environment?" % run["id"], request_owner="account-one",
    )
    assert "clarification" in clarified
    assert "/autopilot" in server._autopilot_command("help", request_owner=None)
    assert "steer" in server._autopilot_command("help", request_owner=None)
