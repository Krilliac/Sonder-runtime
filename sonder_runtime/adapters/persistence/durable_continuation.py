"""SQLite persistence adapter for durable child-session continuation."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from threading import Lock

from sonder_runtime.application.ports.subagents import (
    InvalidSubagentRequest, SubagentBudget, SubagentError, SubagentRequest,
    SubagentResult, SubagentStatus, SubagentUsage, TERMINAL_SUBAGENT_STATUSES,
)
from sonder_runtime.application.subagents.continuable import ContinuableCheckpoint
from sonder_runtime.application.subagents.durable_continuation import (
    ChildSessionLineage, DurableChildSession, DurableContinuationRepository,
)


_DDL = """
CREATE TABLE IF NOT EXISTS durable_child_session (
    child_id TEXT PRIMARY KEY, parent_id TEXT NOT NULL, ancestors_json TEXT NOT NULL,
    prompt TEXT NOT NULL, budget_json TEXT NOT NULL, metadata_json TEXT NOT NULL,
    status TEXT NOT NULL, checkpoint_sequence INTEGER, checkpoint_state_json TEXT,
    checkpoint_cursor TEXT, revision INTEGER NOT NULL, usage_json TEXT NOT NULL,
    result_json TEXT, recovery_required INTEGER NOT NULL,
    cancellation_requested INTEGER NOT NULL, cancellation_reason TEXT
);
"""


def _budget_json(budget: SubagentBudget) -> str:
    return json.dumps({name: getattr(budget, name) for name in (
        "max_children", "max_steps", "max_wall_seconds", "max_output_tokens"
    )}, sort_keys=True, separators=(",", ":"))


def _usage_json(usage: SubagentUsage) -> str:
    return json.dumps({"steps": usage.steps, "output_tokens": usage.output_tokens,
                       "wall_seconds": usage.wall_seconds}, separators=(",", ":"))


def _result_json(result: SubagentResult | None) -> str | None:
    if result is None:
        return None
    return json.dumps({"child_id": result.child_id, "parent_id": result.parent_id,
                       "status": result.status.value, "output": result.output,
                       "error": None if result.error is None else {
                           "code": result.error.code, "message": result.error.message,
                           "retryable": result.error.retryable,
                       }, "usage": json.loads(_usage_json(result.usage))},
                      sort_keys=True, separators=(",", ":"))


def _decode_result(raw: str | None) -> SubagentResult | None:
    if raw is None:
        return None
    value = json.loads(raw)
    error = value["error"]
    return SubagentResult(value["child_id"], value["parent_id"], SubagentStatus(value["status"]),
                          value["output"], None if error is None else SubagentError(
                              error["code"], error["message"], error["retryable"]),
                          SubagentUsage(**value["usage"]))


class SQLiteDurableContinuationRepository:
    """SQLite implementation with transaction-scoped checkpoint CAS."""

    def __init__(self, db_path: str | Path) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        with self._connect() as connection:
            connection.executescript(_DDL)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self._path), timeout=5.0)
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    @staticmethod
    def _row(row: tuple) -> DurableChildSession:
        (child_id, parent_id, ancestors, prompt, budget, metadata, status, sequence,
         state, cursor, revision, usage, result, recovery, cancelling, reason) = row
        request = SubagentRequest(
            parent_id, prompt, SubagentBudget(**json.loads(budget)), child_id,
            tuple(tuple(item) for item in json.loads(metadata)),
        )
        checkpoint = None if sequence is None else ContinuableCheckpoint(
            child_id, sequence, json.loads(state), cursor
        )
        return DurableChildSession(
            request, ChildSessionLineage(parent_id, tuple(json.loads(ancestors))),
            SubagentStatus(status), checkpoint, revision, SubagentUsage(**json.loads(usage)),
            _decode_result(result), bool(recovery), bool(cancelling), reason,
        )

    def _select(self, connection: sqlite3.Connection, child_id: str) -> DurableChildSession | None:
        row = connection.execute(
            "SELECT child_id,parent_id,ancestors_json,prompt,budget_json,metadata_json,status,"
            "checkpoint_sequence,checkpoint_state_json,checkpoint_cursor,revision,usage_json,result_json,"
            "recovery_required,cancellation_requested,cancellation_reason FROM durable_child_session WHERE child_id=?",
            (child_id,),
        ).fetchone()
        return self._row(row) if row else None

    def create(self, session: DurableChildSession) -> DurableChildSession:
        child_id = session.request.child_id
        if child_id is None:
            raise InvalidSubagentRequest("durable child sessions require a child_id")
        with self._lock, self._connect() as connection:
            try:
                connection.execute(
                    "INSERT INTO durable_child_session VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (child_id, session.request.parent_id, json.dumps(session.lineage.ancestors),
                     session.request.prompt, _budget_json(session.request.budget),
                     json.dumps(session.request.metadata), session.status.value,
                     None if session.checkpoint is None else session.checkpoint.sequence,
                     None if session.checkpoint is None else json.dumps(session.checkpoint.state),
                     None if session.checkpoint is None else session.checkpoint.cursor,
                     session.revision, _usage_json(session.usage), _result_json(session.result),
                     int(session.recovery_required), int(session.cancellation_requested),
                     session.cancellation_reason),
                )
            except sqlite3.IntegrityError as exc:
                raise InvalidSubagentRequest("child_id already exists") from exc
        return session

    def get(self, child_id: str) -> DurableChildSession | None:
        with self._connect() as connection:
            return self._select(connection, child_id)

    def save_checkpoint(self, checkpoint: ContinuableCheckpoint, *, expected_sequence: int) -> DurableChildSession | None:
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._select(connection, checkpoint.child_id)
            current_sequence = current.checkpoint.sequence if current and current.checkpoint else -1
            if current is None or current_sequence != expected_sequence or checkpoint.sequence != expected_sequence + 1:
                return None
            connection.execute(
                "UPDATE durable_child_session SET checkpoint_sequence=?,checkpoint_state_json=?,"
                "checkpoint_cursor=?,revision=revision+1 WHERE child_id=? AND revision=?",
                (checkpoint.sequence, json.dumps(checkpoint.state), checkpoint.cursor,
                 checkpoint.child_id, current.revision),
            )
            return self._select(connection, checkpoint.child_id)

    def update(self, child_id: str, *, status: SubagentStatus, expected_revision: int | None = None,
               usage: SubagentUsage | None = None, result: SubagentResult | None = None,
               recovery_required: bool | None = None) -> DurableChildSession | None:
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._select(connection, child_id)
            if current is None or (expected_revision is not None and current.revision != expected_revision):
                return None
            if current.status in TERMINAL_SUBAGENT_STATUSES and status not in TERMINAL_SUBAGENT_STATUSES:
                return current
            connection.execute(
                "UPDATE durable_child_session SET status=?,revision=revision+1,usage_json=?,result_json=?,"
                "recovery_required=? WHERE child_id=? AND revision=?",
                (status.value, _usage_json(usage or current.usage),
                 _result_json(result if result is not None else current.result),
                 int(current.recovery_required if recovery_required is None else recovery_required),
                 child_id, current.revision),
            )
            return self._select(connection, child_id)

    def request_cancel(self, child_id: str, *, reason: str) -> bool:
        if not reason.strip():
            raise InvalidSubagentRequest("cancellation reason is required")
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._select(connection, child_id)
            if current is None:
                raise InvalidSubagentRequest(f"unknown child_id {child_id!r}")
            if current.cancellation_requested or current.status in TERMINAL_SUBAGENT_STATUSES:
                return False
            connection.execute(
                "UPDATE durable_child_session SET cancellation_requested=1,cancellation_reason=?,revision=revision+1 "
                "WHERE child_id=? AND revision=?", (reason, child_id, current.revision)
            )
            return connection.total_changes == 1

    def list_active(self) -> tuple[DurableChildSession, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT child_id,parent_id,ancestors_json,prompt,budget_json,metadata_json,status,"
                "checkpoint_sequence,checkpoint_state_json,checkpoint_cursor,revision,usage_json,result_json,"
                "recovery_required,cancellation_requested,cancellation_reason FROM durable_child_session "
                "WHERE status NOT IN (?,?,?,?) ORDER BY child_id",
                tuple(status.value for status in TERMINAL_SUBAGENT_STATUSES),
            ).fetchall()
        return tuple(self._row(row) for row in rows)


__all__ = ["SQLiteDurableContinuationRepository"]
