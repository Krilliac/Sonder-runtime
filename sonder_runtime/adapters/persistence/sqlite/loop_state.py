"""SQLite adapter for durable loop idempotency and retry evidence."""
from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
from pathlib import Path
import sqlite3
from threading import Lock
from typing import Any

from sonder_runtime.application.loop.durable_control import RetryEvidence
from sonder_runtime.application.persistence.outbox_cas import (
    OutboxCASRepository,
    OutboxEvent,
    PersistenceError,
    TransactionNeutralRecord,
)
from sonder_runtime.domain.loop_retry_policy import RetryDecision


_DDL = """
CREATE TABLE IF NOT EXISTS loop_idempotency_records (
    aggregate_id TEXT PRIMARY KEY,
    revision INTEGER NOT NULL,
    schema_version INTEGER NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS loop_outbox_events (
    event_id TEXT PRIMARY KEY,
    aggregate_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    revision INTEGER NOT NULL,
    schema_version INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    occurred_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS loop_retry_evidence (
    evidence_id INTEGER PRIMARY KEY AUTOINCREMENT,
    operation_id TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    failure_code TEXT NOT NULL,
    action TEXT NOT NULL,
    classification TEXT NOT NULL,
    delay_cap_seconds REAL NOT NULL,
    side_effect TEXT NOT NULL,
    evidence_digest TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_loop_retry_operation
ON loop_retry_evidence(operation_id, evidence_id);
"""


def _digest(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def _jsonable(value: Any) -> Any:
    """Convert persistence-port immutable containers back to JSON values."""
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_jsonable(item) for item in value]
    return value


class SQLiteLoopStateRepository(OutboxCASRepository):
    """Durable CAS/outbox adapter consumed by ``OutboxIdempotencyStore``."""

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
    def _record(row: tuple[Any, ...] | None) -> TransactionNeutralRecord | None:
        if row is None:
            return None
        aggregate_id, revision, schema_version, payload = row
        return TransactionNeutralRecord(
            aggregate_id, revision, json.loads(payload), schema_version=schema_version
        )

    @staticmethod
    def _event(row: tuple[Any, ...]) -> OutboxEvent:
        event_id, aggregate_id, event_type, revision, schema_version, payload, occurred_at = row
        return OutboxEvent(
            event_id, aggregate_id, event_type, revision, json.loads(payload), occurred_at,
            schema_version=schema_version,
        )

    def get(self, aggregate_id: str) -> TransactionNeutralRecord | None:
        with self._connect() as connection:
            return self._record(connection.execute(
                "SELECT aggregate_id,revision,schema_version,payload_json "
                "FROM loop_idempotency_records WHERE aggregate_id=?", (aggregate_id,)
            ).fetchone())

    def append(
        self, record: TransactionNeutralRecord, event: OutboxEvent, *, expected_revision: int
    ) -> TransactionNeutralRecord | None:
        if record.aggregate_id != event.aggregate_id or record.revision != event.revision:
            raise PersistenceError("record and event identity/revision must match")
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._record(connection.execute(
                "SELECT aggregate_id,revision,schema_version,payload_json "
                "FROM loop_idempotency_records WHERE aggregate_id=?", (record.aggregate_id,)
            ).fetchone())
            actual = -1 if current is None else current.revision
            if actual != expected_revision or record.revision != expected_revision + 1:
                return None
            try:
                payload = json.dumps(
                    _jsonable(record.payload), sort_keys=True, separators=(",", ":")
                )
                if current is None:
                    connection.execute(
                        "INSERT INTO loop_idempotency_records VALUES (?,?,?,?)",
                        (record.aggregate_id, record.revision, record.schema_version, payload),
                    )
                else:
                    connection.execute(
                        "UPDATE loop_idempotency_records SET revision=?,schema_version=?,payload_json=? "
                        "WHERE aggregate_id=? AND revision=?",
                        (record.revision, record.schema_version, payload,
                         record.aggregate_id, expected_revision),
                    )
                    if connection.total_changes != 1:
                        return None
                connection.execute(
                    "INSERT INTO loop_outbox_events VALUES (?,?,?,?,?,?,?)",
                    (event.event_id, event.aggregate_id, event.event_type, event.revision,
                     event.schema_version, json.dumps(_jsonable(event.payload), sort_keys=True, separators=(",", ":")),
                     event.occurred_at),
                )
            except sqlite3.IntegrityError:
                return None
            return record

    def outbox(self) -> tuple[OutboxEvent, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT event_id,aggregate_id,event_type,revision,schema_version,payload_json,occurred_at "
                "FROM loop_outbox_events ORDER BY rowid"
            ).fetchall()
        return tuple(self._event(row) for row in rows)


class SQLiteRetryEvidenceLedger:
    """Bounded durable implementation of the loop retry evidence ledger."""

    def __init__(self, db_path: str | Path, *, max_records: int = 256, clock=None) -> None:
        if isinstance(max_records, bool) or max_records < 1:
            raise ValueError("max_records must be positive")
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._max_records = max_records
        self._clock = clock
        self._lock = Lock()
        with sqlite3.connect(str(self._path)) as connection:
            connection.executescript(_DDL)

    def record(
        self, operation_id: str, decision: RetryDecision, *, attempt: int = 1,
        failure_code: str = "",
    ) -> RetryEvidence:
        if not operation_id.strip() or isinstance(attempt, bool) or attempt < 1:
            raise ValueError("operation_id and positive attempt are required")
        recorded_at = self._clock() if self._clock is not None else __import__(
            "datetime"
        ).datetime.now(__import__("datetime").timezone.utc).isoformat()
        digest = _digest({
            "operation_id": operation_id, "failure_code": str(failure_code),
            "action": decision.action.value, "classification": decision.classification.value,
            "side_effect": decision.side_effect.effect.value,
        })
        evidence = RetryEvidence(
            operation_id, attempt, str(failure_code), decision.action,
            decision.classification.value, decision.backoff.cap_for_attempt(1),
            decision.side_effect.effect, digest, recorded_at,
        )
        with self._lock, sqlite3.connect(str(self._path)) as connection:
            connection.execute(
                "INSERT INTO loop_retry_evidence "
                "(operation_id,attempt,failure_code,action,classification,delay_cap_seconds,"
                "side_effect,evidence_digest,recorded_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (evidence.operation_id, evidence.attempt, evidence.failure_code, evidence.action.value,
                 evidence.classification, evidence.delay_cap_seconds, evidence.side_effect.value,
                 evidence.evidence_digest, evidence.recorded_at),
            )
            connection.execute(
                "DELETE FROM loop_retry_evidence WHERE evidence_id NOT IN "
                "(SELECT evidence_id FROM loop_retry_evidence ORDER BY evidence_id DESC LIMIT ?)",
                (self._max_records,),
            )
        return evidence

    def snapshot(self) -> tuple[RetryEvidence, ...]:
        with sqlite3.connect(str(self._path)) as connection:
            rows = connection.execute(
                "SELECT operation_id,attempt,failure_code,action,classification,delay_cap_seconds,"
                "side_effect,evidence_digest,recorded_at FROM loop_retry_evidence ORDER BY evidence_id"
            ).fetchall()
        from sonder_runtime.domain.loop_retry_policy import ReplayAction, SideEffectClass
        return tuple(RetryEvidence(
            operation_id, attempt, failure_code, ReplayAction(action), classification,
            delay, SideEffectClass(side_effect), digest, recorded_at,
        ) for operation_id, attempt, failure_code, action, classification, delay, side_effect, digest, recorded_at in rows)


__all__ = ["SQLiteLoopStateRepository", "SQLiteRetryEvidenceLedger"]
