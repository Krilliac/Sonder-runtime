import json
import sqlite3

import pytest

import sonder_runtime.adapters.persistence.queued_actions as queue
from scripts import package_local_system as package


def test_database_path_uses_packaged_platform_paths(monkeypatch, tmp_path):
    expected = tmp_path / "queued_actions.db"
    calls = []

    def fake_state_path(name, env_var=""):
        calls.append((name, env_var))
        return str(expected)

    monkeypatch.setattr(queue._platform_paths, "state_path", fake_state_path)

    assert queue.database_path() == str(expected)
    assert calls == [("queued_actions.db", "SONDER_QUEUED_ACTION_DB")]


def _proposed(conn, request_id="action-1"):
    request = queue.ActionRequest.create(
        request_id, "write-report", {"document": "weekly", "format": "md"},
        proposed_by=queue.Actor.MODEL,
    )
    return queue.propose(conn, request), request


def _move(conn, action, transition_id, state, actor, **kwargs):
    return queue.transition(conn, queue.TransitionRequest(
        transition_id=transition_id, action_id=action.id,
        expected_version=action.version, to_state=state, actor=actor, **kwargs,
    ))


def test_request_is_immutable_local_bounded_and_idempotent():
    conn = queue.connect(":memory:")
    action, request = _proposed(conn)
    assert action.state is queue.ActionState.PROPOSED
    assert action.version == 1
    assert queue.propose(conn, request) == action
    assert [item.to_state for item in queue.history(conn, action.id)] == [
        queue.ActionState.PROPOSED,
    ]
    with pytest.raises(queue.QueueConflict, match="different content"):
        queue.propose(conn, queue.ActionRequest.create(
            action.id, "write-report", {"document": "different"},
            proposed_by=queue.Actor.MODEL,
        ))
    with pytest.raises(queue.QueueValidation, match="must be local"):
        queue.ActionRequest.create(
            "cloud", "noop", {}, proposed_by=queue.Actor.USER,
            execution_scope="cloud",
        )
    with pytest.raises(queue.QueueValidation, match="exceeds"):
        queue.ActionRequest.create(
            "large", "noop", {"value": "x" * (queue.MAX_STRING_CHARS + 1)},
            proposed_by=queue.Actor.USER,
        )
    assert "queued_actions.py" in package.REQUIRED_FILES
    assert "migrations/queued_actions/0001_baseline.py" in package.REQUIRED_FILES


def test_model_cannot_self_approve_and_only_host_records_execution():
    conn = queue.connect(":memory:")
    action, _ = _proposed(conn)
    _move(conn, action, "pending", queue.ActionState.PENDING_APPROVAL, queue.Actor.HOST)
    action = queue.get_action(conn, action.id)
    with pytest.raises(queue.QueueValidation, match="requires explicit actor=user"):
        _move(conn, action, "approve-model", queue.ActionState.APPROVED, queue.Actor.MODEL)
    approved = _move(
        conn, action, "approve-user", queue.ActionState.APPROVED, queue.Actor.USER,
    )
    action = queue.get_action(conn, action.id)
    assert approved.after_version == action.version
    with pytest.raises(queue.QueueValidation, match="requires explicit actor=host"):
        _move(conn, action, "execute-model", queue.ActionState.EXECUTING, queue.Actor.MODEL)


def test_cas_invalid_transitions_and_exact_replay_are_safe():
    conn = queue.connect(":memory:")
    action, _ = _proposed(conn)
    pending = queue.TransitionRequest(
        transition_id="pending", action_id=action.id, expected_version=1,
        to_state=queue.ActionState.PENDING_APPROVAL, actor=queue.Actor.HOST,
    )
    first = queue.transition(conn, pending)
    assert queue.transition(conn, pending) == first
    with pytest.raises(queue.QueueConflict, match="different content"):
        queue.transition(conn, queue.TransitionRequest(
            transition_id="pending", action_id=action.id, expected_version=1,
            to_state=queue.ActionState.CANCELLED, actor=queue.Actor.USER,
        ))
    with pytest.raises(queue.QueueConflict, match="expected version"):
        queue.transition(conn, queue.TransitionRequest(
            transition_id="stale", action_id=action.id, expected_version=1,
            to_state=queue.ActionState.APPROVED, actor=queue.Actor.USER,
        ))
    current = queue.get_action(conn, action.id)
    with pytest.raises(queue.QueueValidation, match="invalid"):
        _move(conn, current, "skip", queue.ActionState.COMPLETED, queue.Actor.HOST,
              result="done")
    assert queue.get_action(conn, action.id) == current


def test_caller_transition_ids_cannot_collide_with_proposal_namespace():
    conn = queue.connect(":memory:")
    action, _ = _proposed(conn, "existing-action")
    with pytest.raises(queue.QueueValidation, match="reserved proposal"):
        _move(
            conn, action, "proposal:future-request",
            queue.ActionState.PENDING_APPROVAL, queue.Actor.HOST,
        )

    proposed, _ = _proposed(conn, "future-request")
    assert proposed.id == "future-request"
    assert queue.history(conn, proposed.id)[0].transition_id == (
        "proposal:future-request"
    )


def test_full_lifecycle_is_append_only_and_terminal():
    conn = queue.connect(":memory:")
    action, _ = _proposed(conn)
    for transition_id, state, actor, extra in (
        ("pending", queue.ActionState.PENDING_APPROVAL, queue.Actor.HOST, {}),
        ("approved", queue.ActionState.APPROVED, queue.Actor.USER, {}),
        ("executing", queue.ActionState.EXECUTING, queue.Actor.HOST, {}),
        ("completed", queue.ActionState.COMPLETED, queue.Actor.HOST,
         {"result": "report stored by an external host"}),
    ):
        _move(conn, action, transition_id, state, actor, **extra)
        action = queue.get_action(conn, action.id)
    assert action.state is queue.ActionState.COMPLETED
    assert action.version == 5
    assert [item.to_state for item in queue.history(conn, action.id)] == [
        queue.ActionState.PROPOSED,
        queue.ActionState.PENDING_APPROVAL,
        queue.ActionState.APPROVED,
        queue.ActionState.EXECUTING,
        queue.ActionState.COMPLETED,
    ]
    with pytest.raises(queue.QueueValidation, match="invalid"):
        _move(conn, action, "terminal", queue.ActionState.CANCELLED, queue.Actor.USER)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM queued_action_transitions")
    conn.rollback()


def test_executing_cancel_is_requested_then_host_confirms_or_records_reality():
    conn = queue.connect(":memory:")
    pre_execution, _ = _proposed(conn, "cancel-before-execution")
    cancelled = _move(
        conn, pre_execution, "cancel-direct", queue.ActionState.CANCELLED,
        queue.Actor.USER, error="user withdrew request",
    )
    assert cancelled.to_state is queue.ActionState.CANCELLED

    action, _ = _proposed(conn)
    for transition_id, state, actor in (
        ("pending", queue.ActionState.PENDING_APPROVAL, queue.Actor.HOST),
        ("approved", queue.ActionState.APPROVED, queue.Actor.USER),
        ("executing", queue.ActionState.EXECUTING, queue.Actor.HOST),
    ):
        _move(conn, action, transition_id, state, actor)
        action = queue.get_action(conn, action.id)

    with pytest.raises(queue.QueueValidation, match="invalid"):
        _move(
            conn, action, "cancel-without-checkpoint",
            queue.ActionState.CANCELLED, queue.Actor.USER,
        )

    requested = _move(
        conn, action, "cancel-request", queue.ActionState.CANCEL_REQUESTED,
        queue.Actor.USER,
    )
    action = queue.get_action(conn, action.id)
    assert requested.to_state is queue.ActionState.CANCEL_REQUESTED
    assert action.state is queue.ActionState.CANCEL_REQUESTED
    with pytest.raises(queue.QueueValidation, match="requires explicit actor=host"):
        _move(
            conn, action, "self-confirm", queue.ActionState.CANCELLED,
            queue.Actor.USER,
        )
    confirmed = _move(
        conn, action, "host-confirm", queue.ActionState.CANCELLED,
        queue.Actor.HOST, error="cancelled at host checkpoint",
    )
    assert confirmed.to_state is queue.ActionState.CANCELLED

    action, _ = _proposed(conn, "action-completed-late")
    for transition_id, state, actor in (
        ("late-pending", queue.ActionState.PENDING_APPROVAL, queue.Actor.HOST),
        ("late-approved", queue.ActionState.APPROVED, queue.Actor.USER),
        ("late-executing", queue.ActionState.EXECUTING, queue.Actor.HOST),
        ("late-cancel", queue.ActionState.CANCEL_REQUESTED, queue.Actor.USER),
    ):
        _move(conn, action, transition_id, state, actor)
        action = queue.get_action(conn, action.id)
    completed = _move(
        conn, action, "late-completed", queue.ActionState.COMPLETED,
        queue.Actor.HOST, result="side effect already completed",
    )
    assert completed.to_state is queue.ActionState.COMPLETED


@pytest.mark.parametrize(
    ("limit_name", "existing_actions", "existing_transitions", "message"),
    (
        ("MAX_TOTAL_ACTIONS", 1, 1, "total storage budget"),
        ("MAX_OPEN_ACTIONS", 1, 1, "open-action budget"),
        ("MAX_HISTORY_ROWS_FOR_INTAKE", 2, 1, "history intake budget"),
    ),
)
def test_new_proposals_fail_closed_at_storage_budgets(
    monkeypatch, limit_name, existing_actions, existing_transitions, message,
):
    conn = queue.connect(":memory:")
    first, _ = _proposed(conn)
    if existing_actions > 1:
        _proposed(conn, "action-2")
    monkeypatch.setattr(queue, limit_name, existing_transitions)
    with pytest.raises(queue.QueueConflict, match=message):
        _proposed(conn, "over-budget")
    # Exact proposal replay remains available while intake is closed.
    replay = queue.propose(conn, queue.ActionRequest.create(
        first.id, first.action_type, json.loads(first.payload_json),
        proposed_by=first.proposed_by,
    ))
    assert replay.id == first.id


def test_file_store_is_migrated_and_records_schema_ledger(tmp_path):
    path = tmp_path / "queued_actions.db"
    conn = queue.connect(path)
    try:
        migrations = conn.execute(
            "SELECT migration_id FROM schema_migrations"
        ).fetchall()
    finally:
        conn.close()
    assert [row[0] for row in migrations] == ["0001_baseline"]
