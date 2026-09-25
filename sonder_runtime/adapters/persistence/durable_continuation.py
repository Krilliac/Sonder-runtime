"""SQLite persistence adapter for durable child-session continuation."""

from __future__ import annotations

from sonder_runtime.adapters.persistence.owned_sqlite import connect as owned_sqlite_connect
from sonder_runtime.adapters.persistence.owned_sqlite import (
    transaction as owned_sqlite_transaction,
)

from dataclasses import asdict, replace
from collections.abc import Mapping
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
from threading import Lock, Condition
from time import monotonic, sleep
from uuid import uuid4

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
from sonder_runtime.application.execution.effect_journal import (
    EffectJournalError,
    EffectJournalPage,
)
from sonder_runtime.application.subagents.checkpoint_provenance import JournalPosition
from sonder_runtime.application.subagents.continuable import (
    CheckpointProvenance,
    ContinuableCheckpoint,
    provenance_subject_error,
)
from sonder_runtime.application.subagents.durable_continuation import (
    ChildSessionLineage,
    DurableChildSession,
    DurableContinuationRepository,
)
from sonder_runtime.application.subagents.admission import usage_is_monotonic, validate_admission

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
    cancellation_requested INTEGER NOT NULL, cancellation_reason TEXT,
    resume_key TEXT NOT NULL DEFAULT '', idempotency_key TEXT NOT NULL DEFAULT '',
    terminal_verification_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS child_checkpoint_provenance (
    child_id TEXT NOT NULL, sequence INTEGER NOT NULL, version INTEGER NOT NULL,
    state_digest TEXT NOT NULL, cursor TEXT, journal_identity TEXT NOT NULL,
    run_id TEXT NOT NULL, worker_id TEXT NOT NULL, owner_epoch INTEGER NOT NULL,
    settled_position INTEGER NOT NULL, record_digest TEXT NOT NULL,
    PRIMARY KEY (child_id, sequence)
);
"""

# Kept out of ``_DDL``: other tooling splits that script on ``;``, which a
# trigger body contains.  The repository installs these on every open.
_PROVENANCE_TRIGGERS = (
    (
        "CREATE TRIGGER IF NOT EXISTS child_checkpoint_provenance_no_update "
        "BEFORE UPDATE ON child_checkpoint_provenance "
        "BEGIN SELECT RAISE(ABORT, 'checkpoint provenance is immutable'); END"
    ),
    (
        "CREATE TRIGGER IF NOT EXISTS child_checkpoint_provenance_no_delete "
        "BEFORE DELETE ON child_checkpoint_provenance "
        "BEGIN SELECT RAISE(ABORT, 'checkpoint provenance is immutable'); END"
    ),
)

# Every child read joins the provenance stamped for its current checkpoint.
# Rows written before the provenance table existed, or without a host hook,
# have no match and read back provenance-absent.
_SESSION_COLUMNS = (
    "c.child_id,c.parent_id,c.ancestors_json,c.prompt,c.budget_json,c.metadata_json,c.status,"
    "c.checkpoint_sequence,c.checkpoint_state_json,c.checkpoint_cursor,c.revision,c.usage_json,"
    "c.result_json,c.recovery_required,c.cancellation_requested,c.cancellation_reason,"
    "c.resume_key,c.idempotency_key,c.terminal_verification_json,"
    "p.version,p.state_digest,p.cursor,p.journal_identity,p.run_id,p.worker_id,"
    "p.owner_epoch,p.settled_position,p.record_digest"
)
_SESSION_FROM = (
    " FROM durable_child_session c LEFT JOIN child_checkpoint_provenance p"
    " ON p.child_id=c.child_id AND p.sequence=c.checkpoint_sequence "
)
_SESSION_SELECT = "SELECT " + _SESSION_COLUMNS + _SESSION_FROM
_BASE_COLUMN_COUNT = 19


# A public cancellation request may race a worker's terminal update after the
# worker has retained its intent but before its receipt is committed.  Give a
# healthy writer a short, finite opportunity to settle that prior intent; a
# crashed writer still returns the original fail-closed ambiguity.
_CANCELLATION_SETTLEMENT_GRACE_SECONDS = 1.0


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
        self._connections = Condition()
        self._live_connections = 0
        self._admissions_stopped = False
        with self._connect() as connection:
            connection.executescript(_DDL)
            columns = {row[1] for row in connection.execute("PRAGMA table_info(durable_child_session)")}
            if "resume_key" not in columns:
                connection.execute("ALTER TABLE durable_child_session ADD COLUMN resume_key TEXT NOT NULL DEFAULT ''")
            if "idempotency_key" not in columns:
                connection.execute("ALTER TABLE durable_child_session ADD COLUMN idempotency_key TEXT NOT NULL DEFAULT ''")
            if "terminal_verification_json" not in columns:
                connection.execute("ALTER TABLE durable_child_session ADD COLUMN terminal_verification_json TEXT NOT NULL DEFAULT '{}'")
            connection.execute("CREATE INDEX IF NOT EXISTS ix_child_resume_key ON durable_child_session(parent_id,resume_key,status)")
            connection.execute("CREATE INDEX IF NOT EXISTS ix_child_idempotency_key ON durable_child_session(parent_id,idempotency_key,status)")
            for trigger in _PROVENANCE_TRIGGERS:
                connection.execute(trigger)

    @contextmanager
    def _connect(self):
        with self._connections:
            if self._admissions_stopped:
                raise ContinuationStorageFailure("child storage is closed")
            self._live_connections += 1
        connection = None
        try:
            connection = owned_sqlite_connect(str(self._path), timeout=5.0)
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute("PRAGMA foreign_keys=ON")
            if connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name='continuation_migration'"
            ).fetchone():
                migration = connection.execute(
                    "SELECT phase FROM continuation_migration WHERE id=1"
                ).fetchone()
                if migration is None or migration[0] != "ACTIVE":
                    raise ContinuationStorageFailure(
                        "child migration has not activated this database"
                    )
            with connection:
                yield connection
        finally:
            # A failed close deliberately retains the occupied slot: shutdown
            # cannot turn an unproven handle into a successful cleanup claim.
            if connection is not None:
                connection.close()
            with self._connections:
                self._live_connections -= 1
                self._connections.notify_all()

    def stop_admissions(self):
        with self._connections:
            self._admissions_stopped = True

    def close(self, *, runners_stopped=False, timeout=5):
        self.stop_admissions()
        if not runners_stopped:
            return False
        deadline = monotonic() + max(0, min(30, timeout))
        with self._connections:
            while self._live_connections:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    return False
                self._connections.wait(remaining)
            return True

    @staticmethod
    def _row(row: tuple) -> DurableChildSession:
        """Decode a child row, optionally followed by its joined provenance.

        A bare 19-column ``durable_child_session`` row (as the migration
        snapshot reads it) decodes provenance-absent.
        """
        stamp = tuple(row[_BASE_COLUMN_COUNT:])
        row = tuple(row[:_BASE_COLUMN_COUNT])
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
            resume_key,
            idempotency_key,
            terminal_verification,
        ) = row
        request = SubagentRequest(
            parent_id,
            prompt,
            SubagentBudget(**json.loads(budget)),
            child_id,
            tuple(tuple(item) for item in json.loads(metadata)),
            resume_key or "",
            idempotency_key or "",
        )
        provenance = None
        if sequence is not None and stamp and stamp[0] is not None:
            (version, state_digest, stamped_cursor, journal_identity, run_id,
             worker_id, owner_epoch, settled_position, record_digest) = stamp
            provenance = CheckpointProvenance(
                child_id, sequence, state_digest, stamped_cursor, journal_identity,
                run_id, worker_id, owner_epoch, settled_position, record_digest,
                version,
            )
        checkpoint = (
            None
            if sequence is None
            else ContinuableCheckpoint(
                child_id, sequence, json.loads(state), cursor, provenance,
            )
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
            json.loads(terminal_verification or "{}"),
        )

    def _select(
        self, connection: sqlite3.Connection, child_id: str
    ) -> DurableChildSession | None:
        row = connection.execute(
            _SESSION_SELECT + "WHERE c.child_id=?",
            (child_id,),
        ).fetchone()
        return self._row(row) if row else None

    def _apply_create(
        self, connection, session: DurableChildSession
    ) -> DurableChildSession:
        child_id = session.request.child_id
        if child_id is None:
            raise InvalidSubagentRequest("durable child sessions require a child_id")
        validate_admission(session, self._admission_records(connection), new_execution=True)
        active = (SubagentStatus.CREATED.value, SubagentStatus.QUEUED.value, SubagentStatus.RUNNING.value)
        if session.request.resume_key:
            duplicate = connection.execute(
                "SELECT child_id FROM durable_child_session WHERE parent_id=? AND resume_key=? AND status IN (?,?,?) LIMIT 1",
                (session.request.parent_id, session.request.resume_key, *active),
            ).fetchone()
            if duplicate is not None:
                raise InvalidSubagentRequest("active child resume key already exists for parent")
        if session.request.idempotency_key:
            duplicate = connection.execute(
                "SELECT child_id FROM durable_child_session WHERE parent_id=? AND idempotency_key=? AND status IN (?,?,?) LIMIT 1",
                (session.request.parent_id, session.request.idempotency_key, *active),
            ).fetchone()
            if duplicate is not None:
                raise InvalidSubagentRequest("active child idempotency key already exists for parent")
        try:
            connection.execute(
                "INSERT INTO durable_child_session VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
                    session.request.resume_key,
                    session.request.idempotency_key,
                    json.dumps(session.terminal_verification, sort_keys=True, separators=(",", ":")),
                ),
            )
            self._insert_provenance(connection, session.checkpoint)
            if connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name='continuation_migration_watermark'"
            ).fetchone():
                watermark = connection.execute(
                    "SELECT children_high_water FROM continuation_migration_watermark WHERE id=1"
                ).fetchone()
                if watermark is None:
                    raise ContinuationStorageFailure(
                        "child migration watermark is missing"
                    )
                position = connection.execute(
                    "SELECT rowid FROM durable_child_session WHERE child_id=?",
                    (child_id,),
                ).fetchone()[0]
                if position <= watermark[0]:
                    position = watermark[0] + 1
                    connection.execute(
                        "UPDATE durable_child_session SET rowid=? WHERE child_id=?",
                        (position, child_id),
                    )
                connection.execute(
                    "UPDATE continuation_migration_watermark SET children_high_water=? WHERE id=1",
                    (position,),
                )
        except sqlite3.IntegrityError as exc:
            raise InvalidSubagentRequest("child_id already exists") from exc
        return session

    def _admission_records(self, connection) -> tuple[DurableChildSession, ...]:
        rows = connection.execute(
            _SESSION_SELECT
        ).fetchall()
        return tuple(self._row(row) for row in rows)

    @_storage_read
    def get(self, child_id: str) -> DurableChildSession | None:
        with self._connect() as connection:
            return self._select(connection, child_id)

    @_storage_read
    def get_active_by_key(self, parent_id: str, key: str, namespace: str) -> DurableChildSession | None:
        if not isinstance(parent_id, str) or not parent_id.strip() or not isinstance(key, str) or not key.strip():
            raise InvalidSubagentRequest("parent_id and key are required")
        column = {"resume": "resume_key", "idempotency": "idempotency_key"}.get(namespace)
        if column is None:
            raise InvalidSubagentRequest("key namespace must be resume or idempotency")
        with self._connect() as connection:
            row = connection.execute(
                _SESSION_SELECT
                + f"WHERE c.parent_id=? AND c.{column}=? "
                "AND c.status IN (?,?,?) ORDER BY c.child_id LIMIT 1",
                (parent_id, key, SubagentStatus.CREATED.value, SubagentStatus.QUEUED.value, SubagentStatus.RUNNING.value),
            ).fetchone()
        return self._row(row) if row else None

    @_storage_read
    def get_by_key(self, parent_id: str, key: str, namespace: str) -> DurableChildSession | None:
        if not isinstance(parent_id, str) or not parent_id.strip() or not isinstance(key, str) or not key.strip():
            raise InvalidSubagentRequest("parent_id and key are required")
        column = {"resume": "resume_key", "idempotency": "idempotency_key"}.get(namespace)
        if column is None:
            raise InvalidSubagentRequest("key namespace must be resume or idempotency")
        with self._connect() as connection:
            rows = connection.execute(
                _SESSION_SELECT
                + f"WHERE c.parent_id=? AND c.{column}=? ORDER BY c.child_id LIMIT 2",
                (parent_id, key),
            ).fetchall()
        if len(rows) > 1:
            raise InvalidSubagentRequest("ambiguous durable worker key requires explicit recovery")
        return self._row(rows[0]) if rows else None

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
                # Admission preflight cannot establish ordering: another writer
                # may retain an intent before this transaction acquires its lock.
                # Enforce the durable per-child order under the same writer lock
                # that covers state changes and their receipt.
                pending = connection.execute(
                    "SELECT i.kind,i.child_id,i.operation_id,i.payload,i.digest "
                    "FROM continuation_intent i LEFT JOIN continuation_receipt r "
                    "ON r.operation_id=i.operation_id WHERE i.child_id=? "
                    "AND r.operation_id IS NULL ORDER BY i.position LIMIT 1",
                    (prepared.child_id,),
                ).fetchone()
                if pending is not None and pending[2] != prepared.operation_id:
                    raise ContinuationCommitAmbiguous(
                        PreparedContinuationMutation(
                            pending[0],
                            pending[1],
                            pending[2],
                            bytes(pending[3]),
                            pending[4],
                        )
                    )
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
        verification=None,
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
                verification=verification,
            )
        ).value

    def claim_resume(self, child_id, *, expected_revision):
        return self.mutate(
            prepare_call("claim_resume", child_id, expected_revision=expected_revision)
        ).value

    def request_cancel(self, child_id, *, reason, expected_revision=None, unstarted_only=False):
        kwargs = {"reason": reason}
        if unstarted_only or expected_revision is not None:
            kwargs.update(expected_revision=expected_revision, unstarted_only=unstarted_only)
        prepared = prepare_call("request_cancel", child_id, **kwargs)
        deadline = monotonic() + _CANCELLATION_SETTLEMENT_GRACE_SECONDS
        while True:
            try:
                return self.mutate(prepared).value
            except ContinuationCommitAmbiguous as error:
                # Keep this retry scoped to cancellation.  Other mutation
                # callers retain their immediate ambiguity contract, and the
                # same prepared identity prevents duplicate intent rows.
                if error.prepared.child_id != child_id:
                    raise
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise
                if self.reconcile(error.prepared) is not None:
                    continue
                sleep(min(0.01, remaining))

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
        try:
            validate_admission(
                replace(current, checkpoint=checkpoint),
                self._admission_records(connection), resuming=True,
            )
        except InvalidSubagentRequest:
            return None
        # Same transaction as the compare-and-set below and the mutation
        # receipt: a failure anywhere before COMMIT leaves neither.
        self._insert_provenance(connection, checkpoint)
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

    @staticmethod
    def _insert_provenance(connection, checkpoint: ContinuableCheckpoint | None) -> None:
        """Persist host-stamped provenance inside the caller's transaction."""
        if checkpoint is None or checkpoint.provenance is None:
            return
        subject_error = provenance_subject_error(checkpoint)
        if subject_error is not None:
            raise InvalidSubagentRequest(subject_error)
        provenance = checkpoint.provenance
        try:
            connection.execute(
                "INSERT INTO child_checkpoint_provenance(child_id,sequence,version,"
                "state_digest,cursor,journal_identity,run_id,worker_id,owner_epoch,"
                "settled_position,record_digest) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    provenance.child_id, provenance.sequence, provenance.version,
                    provenance.state_digest, provenance.cursor,
                    provenance.journal_identity, provenance.run_id,
                    provenance.worker_id, provenance.owner_epoch,
                    provenance.settled_position, provenance.record_digest,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise InvalidSubagentRequest(
                "checkpoint provenance is already recorded for this sequence"
            ) from exc

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
        verification: Mapping[str, object] | None = None,
    ) -> DurableChildSession | None:
        current = self._select(connection, child_id)
        if current is None or (
            expected_revision is not None and current.revision != expected_revision
        ):
            return None
        next_usage = usage if usage is not None else current.usage
        if not usage_is_monotonic(current.usage, next_usage):
            return None
        if current.result is not None and result is None and next_usage != current.usage:
            return None  # Keep the terminal receipt consistent with spent usage.
        if result is not None and (
            result.usage != next_usage or result.status is not status
            or result.child_id != child_id or result.parent_id != current.request.parent_id
        ):
            return None
        # Cancellation is durable admission intent.  A worker may have read it
        # just before another transaction publishes it; success and restart
        # must therefore be fenced inside this same write transaction.
        if current.cancellation_requested and status in (
            SubagentStatus.RUNNING, SubagentStatus.SUCCEEDED,
        ):
            return None
        if (
            current.status in TERMINAL_SUBAGENT_STATUSES
            and status != current.status
        ):
            return None
        if status is SubagentStatus.SUCCEEDED and current.status not in TERMINAL_SUBAGENT_STATUSES:
            validate_admission(
                replace(current, status=status, usage=next_usage,
                        result=result, recovery_required=False),
                self._admission_records(connection), resuming=True,
            )
        connection.execute(
            "UPDATE durable_child_session SET status=?,revision=revision+1,usage_json=?,result_json=?,"
            "recovery_required=?,terminal_verification_json=? WHERE child_id=? AND revision=?",
            (
                status.value,
                _usage_json(usage or current.usage),
                _result_json(result if result is not None else current.result),
                int(
                    current.recovery_required
                    if recovery_required is None
                    else recovery_required
                ),
                json.dumps(
                    current.terminal_verification if verification is None else verification,
                    sort_keys=True,
                    separators=(",", ":"),
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
        current = self._select(connection, child_id)
        if (current is None or current.revision != expected_revision
                or current.status not in {SubagentStatus.FAILED, SubagentStatus.TIMED_OUT}
                or not current.recovery_required or current.cancellation_requested):
            return None
        validate_admission(
            replace(current, status=SubagentStatus.RUNNING, recovery_required=False, result=None),
            self._admission_records(connection), resuming=True, new_execution=True,
        )
        changed = connection.execute(
            "UPDATE durable_child_session SET status=?,revision=revision+1,"
            "result_json=NULL,recovery_required=0,terminal_verification_json='{}' "
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

    def _apply_request_cancel(self, connection, child_id: str, *, reason: str,
                              expected_revision: int | None = None,
                              unstarted_only: bool = False) -> bool:
        if not reason.strip():
            raise InvalidSubagentRequest("cancellation reason is required")
        current = self._select(connection, child_id)
        if current is None:
            raise InvalidSubagentRequest(f"unknown child_id {child_id!r}")
        if (
            current.cancellation_requested
            or current.status in TERMINAL_SUBAGENT_STATUSES
            or (unstarted_only and (
                current.status not in {SubagentStatus.CREATED, SubagentStatus.QUEUED}
                or current.revision != expected_revision
            ))
        ):
            return False
        if current.status in {SubagentStatus.CREATED, SubagentStatus.QUEUED}:
            # A reservation has no runner to observe cooperative cancellation.
            # Settle it here so it cannot indefinitely own budget or files.
            result = SubagentResult(
                child_id, current.request.parent_id, SubagentStatus.CANCELLED,
                error=SubagentError("cancelled_before_start", reason), usage=current.usage,
            )
            changed = connection.execute(
                "UPDATE durable_child_session SET status=?,result_json=?,"
                "cancellation_requested=1,cancellation_reason=?,revision=revision+1 "
                "WHERE child_id=? AND revision=?",
                (SubagentStatus.CANCELLED.value, _result_json(result), reason, child_id, current.revision),
            )
        else:
            changed = connection.execute(
                "UPDATE durable_child_session SET cancellation_requested=1,cancellation_reason=?,revision=revision+1 "
                "WHERE child_id=? AND revision=?",
                (reason, child_id, current.revision),
            )
        return changed.rowcount == 1

    @_storage_read
    def list_active(self) -> tuple[DurableChildSession, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                _SESSION_SELECT
                + "WHERE c.status NOT IN (?,?,?,?) ORDER BY c.child_id",
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
                _SESSION_SELECT
                + "ORDER BY c.rowid LIMIT ?",
                (limit,),
            ).fetchall()
        return tuple(self._row(row) for row in rows)


_JOURNAL_IDENTITY_DDL = (
    "CREATE TABLE IF NOT EXISTS effect_journal_identity("
    "id INTEGER PRIMARY KEY CHECK(id=1), identity TEXT NOT NULL)"
)
_UNRESOLVED_EFFECT_STATES = ("intent", "uncertain")


class SQLiteJournalProvenanceSource:
    """Read-only journal position source over a SQLite effect journal file.

    A journal needs a durable identity so that a checkpoint stamped against
    one file cannot be validated against a different (swapped or recreated)
    one.  The identity is a random value stored in the journal file itself in
    an additive table the journal ignores.  Only trusted host composition
    passes ``create_identity=True``; validation reads never mint one, so a
    missing or recreated journal refuses instead of silently matching.
    """

    def __init__(self, journal, *, create_identity: bool = False) -> None:
        path = getattr(journal, "database_path", None)
        if not isinstance(path, Path):
            raise TypeError("journal must expose its SQLite database_path")
        for name in ("effects_since", "settled_high_water"):
            if not callable(getattr(journal, name, None)):
                raise TypeError("journal does not implement the effect reader contract")
        if type(create_identity) is not bool:
            raise TypeError("create_identity must be a boolean")
        self._journal, self._path = journal, path
        if create_identity:
            self._ensure_identity()

    def _ensure_identity(self) -> str:
        try:
            with owned_sqlite_transaction(str(self._path), timeout=5.0) as connection:
                connection.execute("PRAGMA busy_timeout=5000")
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(_JOURNAL_IDENTITY_DDL)
                connection.execute(
                    "INSERT OR IGNORE INTO effect_journal_identity(id,identity) VALUES(1,?)",
                    ("journal-" + uuid4().hex,),
                )
                return str(connection.execute(
                    "SELECT identity FROM effect_journal_identity WHERE id=1"
                ).fetchone()[0])
        except sqlite3.Error as exc:
            raise EffectJournalError("effect journal identity is unavailable") from exc

    def position(self, run_id: str, worker_id: str) -> JournalPosition | None:
        """Read identity, current owner epoch and high-water in one snapshot."""
        for value in (run_id, worker_id):
            if not isinstance(value, str) or not value.strip():
                raise EffectJournalError("journal position requires run and worker ids")
        if not self._path.is_file():
            return None
        try:
            uri = self._path.resolve().as_uri() + "?mode=ro"
            with owned_sqlite_transaction(uri, uri=True, timeout=5.0) as connection:
                connection.execute("PRAGMA busy_timeout=5000")
                connection.execute("BEGIN")
                if connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='effect_journal_identity'"
                ).fetchone() is None:
                    return None
                identity = connection.execute(
                    "SELECT identity FROM effect_journal_identity WHERE id=1"
                ).fetchone()
                if identity is None:
                    return None
                owner = connection.execute(
                    "SELECT owner_epoch FROM effect_owner WHERE run_id=? AND worker_id=?",
                    (run_id, worker_id),
                ).fetchone()
                high_water = int(connection.execute(
                    "SELECT COALESCE(MAX(sequence),0) FROM effect_journal WHERE run_id=?",
                    (run_id,),
                ).fetchone()[0])
                first_unresolved = connection.execute(
                    "SELECT MIN(sequence) FROM effect_journal WHERE run_id=? AND state IN (?,?)",
                    (run_id, *_UNRESOLVED_EFFECT_STATES),
                ).fetchone()[0]
        except sqlite3.Error as exc:
            raise EffectJournalError("effect journal position is unavailable") from exc
        settled = high_water if first_unresolved is None else int(first_unresolved) - 1
        return JournalPosition(
            str(identity[0]), run_id, worker_id,
            None if owner is None else int(owner[0]), settled, high_water,
        )

    def settled_high_water(self, run_id: str) -> int:
        try:
            return self._journal.settled_high_water(run_id)
        except sqlite3.Error as exc:
            raise EffectJournalError("effect journal is unavailable") from exc

    def effects_since(self, run_id: str, after_sequence: int, *, limit: int = 100,
                      worker_id: str | None = None) -> EffectJournalPage:
        if not self._path.is_file():
            raise EffectJournalError("effect journal file is missing")
        try:
            return self._journal.effects_since(
                run_id, after_sequence, limit=limit, worker_id=worker_id,
            )
        except sqlite3.Error as exc:
            raise EffectJournalError("effect journal is unavailable") from exc


__all__ = ["SQLiteDurableContinuationRepository", "SQLiteJournalProvenanceSource"]
