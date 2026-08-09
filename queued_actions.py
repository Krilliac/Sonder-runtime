"""Inert, local-only queued-action lifecycle ledger.

This module stores intent and approval state only. It has no executor, scheduler,
MCP registration, permission bridge, cloud path, or autonomous polling loop.
Models may propose immutable requests, but only the exact ``Actor.USER`` value
can approve them and only ``Actor.HOST`` can record execution outcomes.
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any

import sonder_paths


MAX_REQUEST_ID = 128
MAX_TRANSITION_ID = 160
PROPOSAL_TRANSITION_PREFIX = "proposal:"
MAX_ACTION_TYPE = 80
MAX_PAYLOAD_CHARS = 16384
MAX_PAYLOAD_DEPTH = 8
MAX_PAYLOAD_ITEMS = 256
MAX_STRING_CHARS = 4096
MAX_RESULT_CHARS = 16384
MAX_ERROR_CHARS = 4096
MAX_HISTORY_LIMIT = 200
# Intake ceilings keep the append-only database finite without deleting audit
# history. Existing actions may always finish after intake closes; with at most
# six transitions per action, MAX_TOTAL_ACTIONS also gives a hard upper bound of
# 60,000 transition rows. Rotation is an attended operator operation.
MAX_TOTAL_ACTIONS = 10_000
MAX_OPEN_ACTIONS = 1_000
MAX_HISTORY_ROWS_FOR_INTAKE = 50_000


class QueueConflict(RuntimeError):
    """A CAS failed or an idempotency key was reused with different content."""


class QueueValidation(ValueError):
    """A queued-action request or transition is invalid."""


class ActionState(str, Enum):
    PROPOSED = "proposed"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    EXECUTING = "executing"
    CANCEL_REQUESTED = "cancel_requested"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Actor(str, Enum):
    MODEL = "model"
    USER = "user"
    HOST = "host"


@dataclass(frozen=True, slots=True)
class ActionRequest:
    request_id: str
    action_type: str
    payload_json: str
    proposed_by: Actor
    execution_scope: str = "local"

    @classmethod
    def create(
        cls, request_id: str, action_type: str, payload: dict[str, Any],
        *, proposed_by: Actor, execution_scope: str = "local",
    ) -> "ActionRequest":
        return cls(
            request_id=_text(request_id, "request_id", MAX_REQUEST_ID),
            action_type=_text(action_type, "action_type", MAX_ACTION_TYPE),
            payload_json=_canonical_payload(payload),
            proposed_by=_actor(proposed_by),
            execution_scope=_scope(execution_scope),
        )


@dataclass(frozen=True, slots=True)
class TransitionRequest:
    transition_id: str
    action_id: str
    expected_version: int
    to_state: ActionState
    actor: Actor
    result: str = ""
    error: str = ""


@dataclass(frozen=True, slots=True)
class ActionRecord:
    id: str
    action_type: str
    payload_json: str
    proposed_by: Actor
    execution_scope: str
    state: ActionState
    version: int
    created: float
    updated: float


@dataclass(frozen=True, slots=True)
class TransitionRecord:
    transition_id: str
    action_id: str
    from_state: ActionState | None
    to_state: ActionState
    expected_version: int
    after_version: int
    actor: Actor
    result: str
    error: str
    request_digest: str
    created: float


_SCHEMA = """
CREATE TABLE IF NOT EXISTS queued_actions (
    id TEXT PRIMARY KEY,
    action_type TEXT NOT NULL,
    payload_json TEXT NOT NULL CHECK(length(payload_json) <= 16384),
    request_digest TEXT NOT NULL CHECK(length(request_digest) = 64),
    proposed_by TEXT NOT NULL CHECK(proposed_by IN ('model', 'user', 'host')),
    execution_scope TEXT NOT NULL CHECK(execution_scope = 'local'),
    state TEXT NOT NULL CHECK(state IN (
        'proposed', 'pending_approval', 'approved', 'executing',
        'cancel_requested', 'completed', 'failed', 'cancelled'
    )),
    version INTEGER NOT NULL CHECK(version >= 1),
    created REAL NOT NULL,
    updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS queued_action_transitions (
    transition_id TEXT PRIMARY KEY CHECK(length(transition_id) BETWEEN 1 AND 160),
    action_id TEXT NOT NULL,
    from_state TEXT CHECK(from_state IS NULL OR from_state IN (
        'proposed', 'pending_approval', 'approved', 'executing',
        'cancel_requested', 'completed', 'failed', 'cancelled'
    )),
    to_state TEXT NOT NULL CHECK(to_state IN (
        'proposed', 'pending_approval', 'approved', 'executing',
        'cancel_requested', 'completed', 'failed', 'cancelled'
    )),
    expected_version INTEGER NOT NULL CHECK(expected_version >= 0),
    after_version INTEGER NOT NULL CHECK(after_version = expected_version + 1),
    actor TEXT NOT NULL CHECK(actor IN ('model', 'user', 'host')),
    result TEXT NOT NULL DEFAULT '' CHECK(length(result) <= 16384),
    error TEXT NOT NULL DEFAULT '' CHECK(length(error) <= 4096),
    request_digest TEXT NOT NULL CHECK(length(request_digest) = 64),
    created REAL NOT NULL,
    FOREIGN KEY(action_id) REFERENCES queued_actions(id),
    UNIQUE(action_id, after_version),
    CHECK (
        (to_state = 'completed' AND length(result) > 0 AND error = '') OR
        (to_state = 'failed' AND result = '' AND length(error) > 0) OR
        (to_state = 'cancelled' AND result = '') OR
        (to_state NOT IN ('completed', 'failed', 'cancelled') AND result = '' AND error = '')
    ),
    CHECK (
        (to_state = 'approved' AND actor = 'user') OR
        (to_state = 'cancel_requested' AND from_state = 'executing' AND actor = 'user') OR
        (to_state = 'cancelled' AND actor = 'user' AND from_state IN (
            'proposed', 'pending_approval', 'approved'
        )) OR
        (to_state = 'cancelled' AND actor = 'host' AND from_state = 'cancel_requested') OR
        (to_state IN ('pending_approval', 'executing', 'completed', 'failed') AND actor = 'host') OR
        (to_state = 'proposed' AND from_state IS NULL)
    )
);
CREATE INDEX IF NOT EXISTS queued_actions_state_updated
ON queued_actions(state, updated);
CREATE INDEX IF NOT EXISTS queued_action_transitions_action
ON queued_action_transitions(action_id, after_version);
CREATE TRIGGER IF NOT EXISTS queued_action_transitions_no_update
BEFORE UPDATE ON queued_action_transitions BEGIN
    SELECT RAISE(ABORT, 'queued action transition history is append-only');
END;
CREATE TRIGGER IF NOT EXISTS queued_action_transitions_no_delete
BEFORE DELETE ON queued_action_transitions BEGIN
    SELECT RAISE(ABORT, 'queued action transition history is append-only');
END;
"""


_ALLOWED = {
    ActionState.PROPOSED: frozenset({ActionState.PENDING_APPROVAL, ActionState.CANCELLED}),
    ActionState.PENDING_APPROVAL: frozenset({ActionState.APPROVED, ActionState.CANCELLED}),
    ActionState.APPROVED: frozenset({ActionState.EXECUTING, ActionState.CANCELLED}),
    ActionState.EXECUTING: frozenset({
        ActionState.COMPLETED, ActionState.FAILED, ActionState.CANCEL_REQUESTED,
    }),
    ActionState.CANCEL_REQUESTED: frozenset({
        ActionState.COMPLETED, ActionState.FAILED, ActionState.CANCELLED,
    }),
    ActionState.COMPLETED: frozenset(),
    ActionState.FAILED: frozenset(),
    ActionState.CANCELLED: frozenset(),
}


def database_path():
    return sonder_paths.state_path("queued_actions.db", "SONDER_QUEUED_ACTION_DB")


def connect(path=None):
    """Open a migrated local queue database (direct schema only for :memory:)."""
    path = database_path() if path is None else path
    if str(path) == ":memory:":
        conn = sqlite3.connect(path, timeout=5.0)
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(_SCHEMA)
    else:
        import sonder_migrations

        status = sonder_migrations.status("queued_actions", str(path))
        if not status.current:
            sonder_migrations.migrate_store("queued_actions", str(path))
        conn = sonder_migrations.open_connection(str(path), busy_timeout_ms=5000)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _text(value, field, maximum, *, required=True):
    if not isinstance(value, str):
        raise QueueValidation("%s must be a string" % field)
    value = value.strip()
    if required and not value:
        raise QueueValidation("%s must not be empty" % field)
    if len(value) > maximum:
        raise QueueValidation("%s exceeds %d characters" % (field, maximum))
    return value


def _scope(value):
    if value != "local":
        raise QueueValidation("execution_scope must be local")
    return value


def _actor(value):
    if not isinstance(value, Actor):
        raise QueueValidation("actor must be an explicit Actor value")
    return value


def _state(value):
    if not isinstance(value, ActionState):
        raise QueueValidation("to_state must be an ActionState value")
    return value


def _version(value):
    if isinstance(value, bool) or not isinstance(value, int):
        raise QueueValidation("expected_version must be an integer")
    if value < 1:
        raise QueueValidation("expected_version must be at least 1")
    return value


def _validate_json(value, depth=0, counter=None):
    if depth > MAX_PAYLOAD_DEPTH:
        raise QueueValidation("payload exceeds maximum nesting depth")
    if counter is None:
        counter = [0]
    counter[0] += 1
    if counter[0] > MAX_PAYLOAD_ITEMS:
        raise QueueValidation("payload exceeds maximum item count")
    if value is None or isinstance(value, (bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise QueueValidation("payload numbers must be finite")
        return
    if isinstance(value, str):
        if len(value) > MAX_STRING_CHARS:
            raise QueueValidation("payload string exceeds %d characters" % MAX_STRING_CHARS)
        return
    if isinstance(value, list):
        for item in value:
            _validate_json(item, depth + 1, counter)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or not key or len(key) > 128:
                raise QueueValidation("payload keys must be non-empty strings up to 128 characters")
            _validate_json(item, depth + 1, counter)
        return
    raise QueueValidation("payload contains a non-JSON value")


def _canonical_payload(payload):
    if not isinstance(payload, dict):
        raise QueueValidation("payload must be a JSON object")
    _validate_json(payload)
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    )
    if len(encoded) > MAX_PAYLOAD_CHARS:
        raise QueueValidation("payload exceeds %d characters" % MAX_PAYLOAD_CHARS)
    return encoded


def _digest(payload):
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _action(row):
    if row is None:
        raise QueueValidation("queued action does not exist")
    return ActionRecord(
        id=row["id"], action_type=row["action_type"], payload_json=row["payload_json"],
        proposed_by=Actor(row["proposed_by"]), execution_scope=row["execution_scope"],
        state=ActionState(row["state"]), version=int(row["version"]),
        created=float(row["created"]), updated=float(row["updated"]),
    )


def _transition(row):
    return TransitionRecord(
        transition_id=row["transition_id"], action_id=row["action_id"],
        from_state=ActionState(row["from_state"]) if row["from_state"] else None,
        to_state=ActionState(row["to_state"]),
        expected_version=int(row["expected_version"]),
        after_version=int(row["after_version"]), actor=Actor(row["actor"]),
        result=row["result"], error=row["error"],
        request_digest=row["request_digest"], created=float(row["created"]),
    )


def get_action(conn, action_id):
    action_id = _text(action_id, "action_id", MAX_REQUEST_ID)
    return _action(conn.execute(
        "SELECT * FROM queued_actions WHERE id=?", (action_id,),
    ).fetchone())


def propose(conn, request: ActionRequest) -> ActionRecord:
    """Create or idempotently replay one immutable proposed request."""
    if not isinstance(request, ActionRequest):
        raise QueueValidation("request must be an ActionRequest")
    # Revalidate fields even when a caller bypasses ActionRequest.create().
    request_id = _text(request.request_id, "request_id", MAX_REQUEST_ID)
    action_type = _text(request.action_type, "action_type", MAX_ACTION_TYPE)
    proposed_by = _actor(request.proposed_by)
    scope = _scope(request.execution_scope)
    try:
        parsed_payload = json.loads(request.payload_json)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise QueueValidation("payload_json must contain one JSON object") from exc
    payload_json = _canonical_payload(parsed_payload)
    request_digest = _digest({
        "id": request_id, "action_type": action_type, "payload_json": payload_json,
        "proposed_by": proposed_by.value, "execution_scope": scope,
    })
    if conn.in_transaction:
        raise QueueConflict("proposal requires a connection outside another transaction")
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT * FROM queued_actions WHERE id=?", (request_id,),
        ).fetchone()
        if existing is not None:
            if existing["request_digest"] != request_digest:
                raise QueueConflict("request_id was replayed with different content")
            conn.commit()
            return _action(existing)
        counts = conn.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM queued_actions) AS total_actions,
                (SELECT COUNT(*) FROM queued_actions WHERE state NOT IN (
                    'completed', 'failed', 'cancelled'
                )) AS open_actions,
                (SELECT COUNT(*) FROM queued_action_transitions) AS history_rows
            """
        ).fetchone()
        if int(counts["total_actions"]) >= MAX_TOTAL_ACTIONS:
            raise QueueConflict(
                "queued-action total storage budget reached; archive/rotate the "
                "fully terminal ledger before accepting new proposals"
            )
        if int(counts["open_actions"]) >= MAX_OPEN_ACTIONS:
            raise QueueConflict(
                "queued-action open-action budget reached; finish or cancel "
                "existing work before accepting new proposals"
            )
        if int(counts["history_rows"]) >= MAX_HISTORY_ROWS_FOR_INTAKE:
            raise QueueConflict(
                "queued-action history intake budget reached; archive/rotate the "
                "fully terminal ledger before accepting new proposals"
            )
        now = time.time()
        conn.execute(
            "INSERT INTO queued_actions(id, action_type, payload_json, request_digest, "
            "proposed_by, execution_scope, state, version, created, updated) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
            (
                request_id, action_type, payload_json, request_digest,
                proposed_by.value, scope, ActionState.PROPOSED.value, now, now,
            ),
        )
        transition_id = PROPOSAL_TRANSITION_PREFIX + request_id
        conn.execute(
            "INSERT INTO queued_action_transitions(transition_id, action_id, "
            "from_state, to_state, expected_version, after_version, actor, "
            "result, error, request_digest, created) "
            "VALUES(?, ?, NULL, ?, 0, 1, ?, '', '', ?, ?)",
            (transition_id, request_id, ActionState.PROPOSED.value, proposed_by.value,
             request_digest, now),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return get_action(conn, request_id)


def _required_actor(from_state, to_state):
    if to_state is ActionState.APPROVED:
        return Actor.USER
    if to_state is ActionState.CANCEL_REQUESTED:
        return Actor.USER
    if to_state is ActionState.CANCELLED:
        return Actor.HOST if from_state is ActionState.CANCEL_REQUESTED else Actor.USER
    if from_state is ActionState.PROPOSED and to_state is ActionState.PENDING_APPROVAL:
        return Actor.HOST
    if to_state in (ActionState.EXECUTING, ActionState.COMPLETED, ActionState.FAILED):
        return Actor.HOST
    return None


def _transition_digest(request, result, error):
    return _digest({
        "transition_id": request.transition_id,
        "action_id": request.action_id,
        "expected_version": request.expected_version,
        "to_state": request.to_state.value,
        "actor": request.actor.value,
        "result": result,
        "error": error,
    })


def transition(conn, request: TransitionRequest) -> TransitionRecord:
    """Apply one legal CAS transition or replay its exact idempotency key."""
    if not isinstance(request, TransitionRequest):
        raise QueueValidation("request must be a TransitionRequest")
    transition_id = _text(request.transition_id, "transition_id", MAX_TRANSITION_ID)
    if transition_id.startswith(PROPOSAL_TRANSITION_PREFIX):
        raise QueueValidation(
            "transition_id uses the reserved proposal: namespace"
        )
    action_id = _text(request.action_id, "action_id", MAX_REQUEST_ID)
    expected = _version(request.expected_version)
    to_state = _state(request.to_state)
    actor = _actor(request.actor)
    result = _text(request.result, "result", MAX_RESULT_CHARS, required=False)
    error = _text(request.error, "error", MAX_ERROR_CHARS, required=False)
    if to_state is ActionState.COMPLETED:
        if not result or error:
            raise QueueValidation("completed transition requires result and forbids error")
    elif to_state is ActionState.FAILED:
        if not error or result:
            raise QueueValidation("failed transition requires error and forbids result")
    elif to_state is not ActionState.CANCELLED and (result or error):
        raise QueueValidation("only terminal transitions may carry result or error")
    elif to_state is ActionState.CANCELLED and result:
        raise QueueValidation("cancelled transition may carry an error/reason, not result")
    canonical = TransitionRequest(
        transition_id=transition_id, action_id=action_id,
        expected_version=expected, to_state=to_state, actor=actor,
        result=result, error=error,
    )
    digest = _transition_digest(canonical, result, error)
    if conn.in_transaction:
        raise QueueConflict("transition requires a connection outside another transaction")
    try:
        conn.execute("BEGIN IMMEDIATE")
        prior = conn.execute(
            "SELECT * FROM queued_action_transitions WHERE transition_id=?",
            (transition_id,),
        ).fetchone()
        if prior is not None:
            if prior["request_digest"] != digest:
                raise QueueConflict("transition_id was replayed with different content")
            conn.commit()
            return _transition(prior)
        action = get_action(conn, action_id)
        if action.version != expected:
            raise QueueConflict(
                "queued action changed concurrently: expected version %d, found %d"
                % (expected, action.version)
            )
        if to_state not in _ALLOWED[action.state]:
            raise QueueValidation(
                "invalid queued action transition: %s -> %s"
                % (action.state.value, to_state.value)
            )
        required = _required_actor(action.state, to_state)
        if required is not None and actor is not required:
            raise QueueValidation(
                "%s -> %s requires explicit actor=%s; actor=%s cannot authorize it"
                % (action.state.value, to_state.value, required.value, actor.value)
            )
        now = time.time()
        cur = conn.execute(
            "UPDATE queued_actions SET state=?, version=version + 1, updated=? "
            "WHERE id=? AND version=? AND state=?",
            (to_state.value, now, action.id, expected, action.state.value),
        )
        if cur.rowcount != 1:
            raise QueueConflict("queued action changed during compare-and-swap")
        conn.execute(
            "INSERT INTO queued_action_transitions(transition_id, action_id, "
            "from_state, to_state, expected_version, after_version, actor, "
            "result, error, request_digest, created) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                transition_id, action.id, action.state.value, to_state.value,
                expected, expected + 1, actor.value, result, error, digest, now,
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return _transition(conn.execute(
        "SELECT * FROM queued_action_transitions WHERE transition_id=?",
        (transition_id,),
    ).fetchone())


def history(conn, action_id, limit=50):
    action_id = _text(action_id, "action_id", MAX_REQUEST_ID)
    try:
        limit = int(limit)
    except (TypeError, ValueError) as exc:
        raise QueueValidation("history limit must be an integer") from exc
    limit = max(1, min(limit, MAX_HISTORY_LIMIT))
    rows = conn.execute(
        "SELECT * FROM queued_action_transitions WHERE action_id=? "
        "ORDER BY after_version ASC LIMIT ?", (action_id, limit),
    ).fetchall()
    return [_transition(row) for row in rows]
