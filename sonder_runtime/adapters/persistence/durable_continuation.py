"""SQLite persistence adapter for durable child-session continuation."""

from __future__ import annotations

from dataclasses import asdict
from contextlib import contextmanager
from functools import wraps
from ...application.ports.continuation_mutations import (
    PreparedContinuationMutation,
    ContinuationMutationOutcome,
    ContinuationCommitAmbiguous,
    ContinuationReceiptCapacity,
    ContinuationStorageFailure,
    prepare_call,
    canonical,
)
from ...application.subagents.continuation_codec import decode_call
import json
import sqlite3
from pathlib import Path
from threading import Lock

from sonder_runtime.application.ports.subagents import (
    InvalidSubagentRequest,
    SubagentBudget,
    SubagentError,
    SubagentRequest,
    SubagentResult,
    SubagentStatus,
    SubagentUsage,
    TERMINAL_SUBAGENT_STATUSES,
)
from sonder_runtime.application.subagents.continuable import ContinuableCheckpoint
from sonder_runtime.application.subagents.durable_continuation import (
    ChildSessionLineage,
    DurableChildSession,
    DurableContinuationRepository,
)

_DDL = """
CREATE TABLE IF NOT EXISTS continuation_intent(position INTEGER PRIMARY KEY AUTOINCREMENT, operation_id TEXT UNIQUE NOT NULL, child_id TEXT NOT NULL, kind TEXT NOT NULL, digest TEXT NOT NULL, payload BLOB NOT NULL);
CREATE INDEX IF NOT EXISTS continuation_intent_child ON continuation_intent(child_id,position);
CREATE TABLE IF NOT EXISTS continuation_receipt(operation_id TEXT PRIMARY KEY REFERENCES continuation_intent(operation_id), disposition TEXT NOT NULL, result BLOB NOT NULL, revision INTEGER);
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
    return json.dumps(
        {
            name: getattr(budget, name)
            for name in (
                "max_children",
                "max_depth",
                "max_concurrency",
                "max_steps",
                "max_wall_seconds",
                "max_output_tokens",
            )
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _usage_json(usage: SubagentUsage) -> str:
    return json.dumps(
        {
            "steps": usage.steps,
            "output_tokens": usage.output_tokens,
            "wall_seconds": usage.wall_seconds,
        },
        separators=(",", ":"),
    )


def _result_json(result: SubagentResult | None) -> str | None:
    if result is None:
        return None
    return json.dumps(
        {
            "child_id": result.child_id,
            "parent_id": result.parent_id,
            "status": result.status.value,
            "output": result.output,
            "error": (
                None
                if result.error is None
                else {
                    "code": result.error.code,
                    "message": result.error.message,
                    "retryable": result.error.retryable,
                }
            ),
            "usage": json.loads(_usage_json(result.usage)),
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _decode_result(raw: str | None) -> SubagentResult | None:
    if raw is None:
        return None
    value = json.loads(raw)
    error = value["error"]
    return SubagentResult(
        value["child_id"],
        value["parent_id"],
        SubagentStatus(value["status"]),
        value["output"],
        (
            None
            if error is None
            else SubagentError(error["code"], error["message"], error["retryable"])
        ),
        SubagentUsage(**value["usage"]),
    )


def _storage_read(method):
    @wraps(method)
    def read(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except sqlite3.Error as error:
            raise ContinuationStorageFailure(
                "continuation storage read unavailable"
            ) from error

    return read


class SQLiteDurableContinuationRepository:
    """SQLite implementation with transaction-scoped checkpoint CAS."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        max_receipts=100_000,
        max_receipt_bytes=64 * 1024 * 1024,
    ) -> None:
        if (
            type(max_receipts) is not int
            or not 1 <= max_receipts <= 100_000
            or type(max_receipt_bytes) is not int
            or not 1 <= max_receipt_bytes <= 64 * 1024 * 1024
        ):
            raise ValueError("invalid continuation receipt capacity")
        self._max_receipts, self._max_receipt_bytes = max_receipts, max_receipt_bytes
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        with self._connect() as connection:
            connection.executescript(_DDL)

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(str(self._path), timeout=5.0)
        try:
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute("PRAGMA foreign_keys=ON")
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _row(row: tuple) -> DurableChildSession:
        (
            child_id,
            parent_id,
            ancestors,
            prompt,
            budget,
            metadata,
            status,
            sequence,
            state,
            cursor,
            revision,
            usage,
            result,
            recovery,
            cancelling,
            reason,
        ) = row
        request = SubagentRequest(
            parent_id,
            prompt,
            SubagentBudget(**json.loads(budget)),
            child_id,
            tuple(tuple(item) for item in json.loads(metadata)),
        )
        checkpoint = (
            None
            if sequence is None
            else ContinuableCheckpoint(child_id, sequence, json.loads(state), cursor)
        )
        return DurableChildSession(
            request,
            ChildSessionLineage(parent_id, tuple(json.loads(ancestors))),
            SubagentStatus(status),
            checkpoint,
            revision,
            SubagentUsage(**json.loads(usage)),
            _decode_result(result),
            bool(recovery),
            bool(cancelling),
            reason,
        )

    def _select(
        self, connection: sqlite3.Connection, child_id: str
    ) -> DurableChildSession | None:
        row = connection.execute(
            "SELECT child_id,parent_id,ancestors_json,prompt,budget_json,metadata_json,status,"
            "checkpoint_sequence,checkpoint_state_json,checkpoint_cursor,revision,usage_json,result_json,"
            "recovery_required,cancellation_requested,cancellation_reason FROM durable_child_session WHERE child_id=?",
            (child_id,),
        ).fetchone()
        return self._row(row) if row else None

    def _apply_create(
        self, connection, session: DurableChildSession
    ) -> DurableChildSession:
        child_id = session.request.child_id
        if child_id is None:
            raise InvalidSubagentRequest("durable child sessions require a child_id")
        try:
            connection.execute(
                "INSERT INTO durable_child_session VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    child_id,
                    session.request.parent_id,
                    json.dumps(session.lineage.ancestors),
                    session.request.prompt,
                    _budget_json(session.request.budget),
                    json.dumps(session.request.metadata),
                    session.status.value,
                    None if session.checkpoint is None else session.checkpoint.sequence,
                    (
                        None
                        if session.checkpoint is None
                        else json.dumps(session.checkpoint.state)
                    ),
                    None if session.checkpoint is None else session.checkpoint.cursor,
                    session.revision,
                    _usage_json(session.usage),
                    _result_json(session.result),
                    int(session.recovery_required),
                    int(session.cancellation_requested),
                    session.cancellation_reason,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise InvalidSubagentRequest("child_id already exists") from exc
        return session

    @_storage_read
    def get(self, child_id: str) -> DurableChildSession | None:
        with self._connect() as connection:
            return self._select(connection, child_id)

    def _capacity(self, connection, extra=0):
        count, size = connection.execute(
            "SELECT COUNT(*),COALESCE(SUM(length(payload)),0) FROM continuation_intent"
        ).fetchone()
        result_size = connection.execute(
            "SELECT COALESCE(SUM(length(result)),0) FROM continuation_receipt"
        ).fetchone()[0]
        if (
            count > self._max_receipts
            or size + result_size + extra > self._max_receipt_bytes
        ):
            raise ContinuationReceiptCapacity("continuation receipt capacity exhausted")

    def _retained(self, connection, prepared):
        row = connection.execute(
            "SELECT digest FROM continuation_intent WHERE operation_id=?",
            (prepared.operation_id,),
        ).fetchone()
        if row and row[0] != prepared.request_sha256:
            raise InvalidSubagentRequest(
                "operation identity already has different input"
            )
        return row is not None

    def _receipt(self, connection, prepared):
        self._retained(connection, prepared)
        row = connection.execute(
            "SELECT disposition,result,revision FROM continuation_receipt WHERE operation_id=?",
            (prepared.operation_id,),
        ).fetchone()
        return (
            ContinuationMutationOutcome(row[0], bytes(row[1]), row[2], True)
            if row
            else None
        )

    @_storage_read
    def reconcile(self, prepared):
        if not isinstance(prepared, PreparedContinuationMutation):
            raise TypeError("prepared mutation required")
        with self._connect() as connection:
            return self._receipt(connection, prepared)

    @_storage_read
    def latest_mutation(self, child_id):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT kind,child_id,operation_id,payload,digest FROM continuation_intent WHERE child_id=? ORDER BY position DESC LIMIT 1",
                (child_id,),
            ).fetchone()
        return (
            PreparedContinuationMutation(row[0], row[1], row[2], bytes(row[3]), row[4])
            if row
            else None
        )

    @_storage_read
    def unresolved_mutation(self, child_id):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT i.kind,i.child_id,i.operation_id,i.payload,i.digest "
                "FROM continuation_intent i LEFT JOIN continuation_receipt r "
                "ON r.operation_id=i.operation_id WHERE i.child_id=? "
                "AND r.operation_id IS NULL ORDER BY i.position LIMIT 1",
                (child_id,),
            ).fetchone()
        return (
            PreparedContinuationMutation(row[0], row[1], row[2], bytes(row[3]), row[4])
            if row
            else None
        )

    @_storage_read
    def read_mutation(self, operation_id):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT kind,child_id,operation_id,payload,digest FROM continuation_intent WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
        return (
            PreparedContinuationMutation(row[0], row[1], row[2], bytes(row[3]), row[4])
            if row
            else None
        )

    @_storage_read
    def mutation_ids(self, child_id, *, after=0, limit=100):
        if (
            type(after) is not int
            or after < 0
            or type(limit) is not int
            or not 1 <= limit <= 100
        ):
            raise ValueError("invalid mutation page bounds")
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT position,operation_id FROM continuation_intent WHERE child_id=? AND position>? ORDER BY position LIMIT ?",
                (child_id, after, limit + 1),
            ).fetchall()
        return tuple(rows[:limit]), len(rows) > limit

    def mutate(self, prepared):
        if not isinstance(prepared, PreparedContinuationMutation):
            raise TypeError("prepared mutation required")
        args, kwargs = decode_call(prepared)
        if (
            prepare_call(
                prepared.kind, *args, operation_id=prepared.operation_id, **kwargs
            )
            != prepared
        ):
            raise InvalidSubagentRequest(
                "prepared mutation arguments do not match identity"
            )
        try:
            # Retain exact intent before attempting the state transaction. A
            # lost process never has to invent the attempted operation identity.
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                if not self._retained(connection, prepared):
                    connection.execute(
                        "INSERT INTO continuation_intent(operation_id,child_id,kind,digest,payload) VALUES(?,?,?,?,?)",
                        (
                            prepared.operation_id,
                            prepared.child_id,
                            prepared.kind,
                            prepared.request_sha256,
                            prepared.payload,
                        ),
                    )
                    self._capacity(connection)
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                prior = self._receipt(connection, prepared)
                if prior is not None:
                    return prior
                connection.execute("SAVEPOINT mutation_effect")
                try:
                    value = getattr(self, "_apply_" + prepared.kind)(
                        connection, *args, **kwargs
                    )
                except InvalidSubagentRequest as error:
                    connection.execute("ROLLBACK TO mutation_effect")
                    disposition, result, revision = (
                        "invalid",
                        canonical({"error": str(error)}),
                        None,
                    )
                else:
                    disposition = (
                        "applied"
                        if value is not None and value is not False
                        else "precondition_failed" if value is None else "no_change"
                    )
                    result = canonical(
                        asdict(value)
                        if isinstance(value, DurableChildSession)
                        else value
                    )
                    revision = (
                        value.revision
                        if isinstance(value, DurableChildSession)
                        else None
                    )
                connection.execute("RELEASE mutation_effect")
                self._capacity(connection, len(result))
                connection.execute(
                    "INSERT INTO continuation_receipt VALUES(?,?,?,?)",
                    (prepared.operation_id, disposition, result, revision),
                )
            return ContinuationMutationOutcome(disposition, result, revision)
        except sqlite3.Error as error:
            raise ContinuationCommitAmbiguous(prepared) from error

    def create(self, session):
        return self.mutate(prepare_call("create", session)).value

    def save_checkpoint(self, checkpoint, *, expected_sequence):
        return self.mutate(
            prepare_call(
                "save_checkpoint", checkpoint, expected_sequence=expected_sequence
            )
        ).value

    def update(
        self,
        child_id,
        *,
        status,
        expected_revision=None,
        usage=None,
        result=None,
        recovery_required=None,
    ):
        return self.mutate(
            prepare_call(
                "update",
                child_id,
                status=status,
                expected_revision=expected_revision,
                usage=usage,
                result=result,
                recovery_required=recovery_required,
            )
        ).value

    def claim_resume(self, child_id, *, expected_revision):
        return self.mutate(
            prepare_call("claim_resume", child_id, expected_revision=expected_revision)
        ).value

    def request_cancel(self, child_id, *, reason):
        return self.mutate(
            prepare_call("request_cancel", child_id, reason=reason)
        ).value

    def _apply_save_checkpoint(
        self, connection, checkpoint: ContinuableCheckpoint, *, expected_sequence: int
    ) -> DurableChildSession | None:
        current = self._select(connection, checkpoint.child_id)
        current_sequence = (
            current.checkpoint.sequence if current and current.checkpoint else -1
        )
        if (
            current is None
            or current_sequence != expected_sequence
            or checkpoint.sequence != expected_sequence + 1
        ):
            return None
        connection.execute(
            "UPDATE durable_child_session SET checkpoint_sequence=?,checkpoint_state_json=?,"
            "checkpoint_cursor=?,revision=revision+1 WHERE child_id=? AND revision=?",
            (
                checkpoint.sequence,
                json.dumps(checkpoint.state),
                checkpoint.cursor,
                checkpoint.child_id,
                current.revision,
            ),
        )
        return self._select(connection, checkpoint.child_id)

    def _apply_update(
        self,
        connection,
        child_id: str,
        *,
        status: SubagentStatus,
        expected_revision: int | None = None,
        usage: SubagentUsage | None = None,
        result: SubagentResult | None = None,
        recovery_required: bool | None = None,
    ) -> DurableChildSession | None:
        current = self._select(connection, child_id)
        if current is None or (
            expected_revision is not None and current.revision != expected_revision
        ):
            return None
        if (
            current.status in TERMINAL_SUBAGENT_STATUSES
            and status not in TERMINAL_SUBAGENT_STATUSES
        ):
            return None
        connection.execute(
            "UPDATE durable_child_session SET status=?,revision=revision+1,usage_json=?,result_json=?,"
            "recovery_required=? WHERE child_id=? AND revision=?",
            (
                status.value,
                _usage_json(usage or current.usage),
                _result_json(result if result is not None else current.result),
                int(
                    current.recovery_required
                    if recovery_required is None
                    else recovery_required
                ),
                child_id,
                current.revision,
            ),
        )
        return self._select(connection, child_id)

    def _apply_claim_resume(
        self, connection, child_id: str, *, expected_revision: int
    ) -> DurableChildSession | None:
        """Claim one recoverable failure without resurrecting other terminal states."""
        changed = connection.execute(
            "UPDATE durable_child_session SET status=?,revision=revision+1,"
            "result_json=NULL,recovery_required=0 "
            "WHERE child_id=? AND revision=? AND status IN (?,?) "
            "AND recovery_required=1 AND cancellation_requested=0",
            (
                SubagentStatus.RUNNING.value,
                child_id,
                expected_revision,
                SubagentStatus.FAILED.value,
                SubagentStatus.TIMED_OUT.value,
            ),
        )
        if changed.rowcount != 1:
            return None
        return self._select(connection, child_id)

    def _apply_request_cancel(self, connection, child_id: str, *, reason: str) -> bool:
        if not reason.strip():
            raise InvalidSubagentRequest("cancellation reason is required")
        current = self._select(connection, child_id)
        if current is None:
            raise InvalidSubagentRequest(f"unknown child_id {child_id!r}")
        if (
            current.cancellation_requested
            or current.status in TERMINAL_SUBAGENT_STATUSES
        ):
            return False
        connection.execute(
            "UPDATE durable_child_session SET cancellation_requested=1,cancellation_reason=?,revision=revision+1 "
            "WHERE child_id=? AND revision=?",
            (reason, child_id, current.revision),
        )
        return connection.total_changes == 1

    @_storage_read
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

    @_storage_read
    def list_all(self, *, limit: int = 1000) -> tuple[DurableChildSession, ...]:
        """Return a bounded operator projection without exposing prompts."""
        if isinstance(limit, bool) or limit < 1:
            raise InvalidSubagentRequest("limit must be positive")
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT child_id,parent_id,ancestors_json,prompt,budget_json,metadata_json,status,"
                "checkpoint_sequence,checkpoint_state_json,checkpoint_cursor,revision,usage_json,result_json,"
                "recovery_required,cancellation_requested,cancellation_reason FROM durable_child_session "
                "ORDER BY rowid LIMIT ?",
                (limit,),
            ).fetchall()
        return tuple(self._row(row) for row in rows)


__all__ = ["SQLiteDurableContinuationRepository"]
