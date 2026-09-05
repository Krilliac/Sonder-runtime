"""SQLite adapter for atomic, idempotent cross-domain record/outbox writes."""
from __future__ import annotations

from sonder_runtime.adapters.persistence.owned_sqlite import connect as owned_sqlite_connect

import hashlib
import json
from pathlib import Path
import re
import sqlite3
from threading import Lock

from ....application.persistence.cross_domain import (
    CoordinationIdempotencyConflict,
    CoordinationResult,
    CoordinationRevisionConflict,
    CrossDomainWrite,
)
from ....application.persistence.outbox_cas import OutboxEvent, TransactionNeutralRecord


_NAME = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_DDL = """
CREATE TABLE IF NOT EXISTS cross_domain_operations (
    operation_id TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    committed_at TEXT NOT NULL
);
"""


def _jsonable(value):
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if hasattr(value, "items"):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_jsonable(item) for item in value]
    return value


def _fingerprint(writes: tuple[CrossDomainWrite, ...]) -> str:
    value = [
        {
            "domain": item.domain,
            "expected_revision": item.expected_revision,
            "record": {
                "aggregate_id": item.record.aggregate_id,
                "revision": item.record.revision,
                "schema_version": item.record.schema_version,
                "payload": _jsonable(item.record.payload),
            },
            "event": {
                "event_id": item.event.event_id,
                "aggregate_id": item.event.aggregate_id,
                "event_type": item.event.event_type,
                "revision": item.event.revision,
                "schema_version": item.event.schema_version,
                "payload": _jsonable(item.event.payload),
                "occurred_at": item.event.occurred_at,
            },
        }
        for item in writes
    ]
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class SQLiteCrossDomainCoordinator:
    """Coordinate participants that share this adapter's SQLite database.

    Domain tables are owned by this adapter's participant namespace, while the
    coordinator owns only the transaction and operation receipt. A stale
    participant or any SQLite error rolls the whole transaction back.
    """

    def __init__(self, db_path: str | Path, *, clock=None) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock or (lambda: "now")
        self._lock = Lock()
        with owned_sqlite_connect(str(self._path)) as connection:
            connection.executescript(_DDL)

    def _connect(self):
        connection = owned_sqlite_connect(str(self._path), timeout=5.0)
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    @staticmethod
    def _tables(domain: str) -> tuple[str, str]:
        if not _NAME.fullmatch(domain):
            raise ValueError("domain must be a lowercase identifier")
        return f"coord_{domain}_records", f"coord_{domain}_outbox"

    def _ensure_domain(self, connection, domain: str) -> tuple[str, str]:
        records, events = self._tables(domain)
        connection.execute(
            f"CREATE TABLE IF NOT EXISTS {records} (aggregate_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, schema_version INTEGER NOT NULL, payload_json TEXT NOT NULL)"
        )
        connection.execute(
            f"CREATE TABLE IF NOT EXISTS {events} (event_id TEXT PRIMARY KEY, aggregate_id TEXT NOT NULL, event_type TEXT NOT NULL, revision INTEGER NOT NULL, schema_version INTEGER NOT NULL, payload_json TEXT NOT NULL, occurred_at TEXT NOT NULL)"
        )
        return records, events

    def coordinate(self, operation_id: str, writes: tuple[CrossDomainWrite, ...]) -> CoordinationResult:
        if not operation_id.strip() or not writes:
            raise ValueError("operation_id and at least one write are required")
        if len({item.domain for item in writes}) != len(writes):
            raise ValueError("one write per domain is required")
        fingerprint = _fingerprint(writes)
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            prior = connection.execute(
                "SELECT fingerprint FROM cross_domain_operations WHERE operation_id=?", (operation_id,)
            ).fetchone()
            if prior is not None:
                if prior[0] != fingerprint:
                    raise CoordinationIdempotencyConflict("operation_id fingerprint conflict")
                return CoordinationResult(operation_id, fingerprint, committed=True, replayed=True)
            try:
                for write in writes:
                    records, events = self._ensure_domain(connection, write.domain)
                    current = connection.execute(
                        f"SELECT revision FROM {records} WHERE aggregate_id=?", (write.record.aggregate_id,)
                    ).fetchone()
                    actual = -1 if current is None else current[0]
                    if actual != write.expected_revision:
                        raise CoordinationRevisionConflict(
                            f"{write.domain}/{write.record.aggregate_id} expected revision "
                            f"{write.expected_revision}, actual {actual}"
                        )
                    payload = json.dumps(_jsonable(write.record.payload), sort_keys=True, separators=(",", ":"))
                    if current is None:
                        connection.execute(
                            f"INSERT INTO {records} VALUES (?,?,?,?)",
                            (write.record.aggregate_id, write.record.revision, write.record.schema_version, payload),
                        )
                    else:
                        connection.execute(
                            f"UPDATE {records} SET revision=?, schema_version=?, payload_json=? "
                            "WHERE aggregate_id=? AND revision=?",
                            (write.record.revision, write.record.schema_version, payload,
                             write.record.aggregate_id, write.expected_revision),
                        )
                        if connection.total_changes < 1:
                            raise CoordinationRevisionConflict(
                                f"{write.domain}/{write.record.aggregate_id} changed during coordination"
                            )
                    event_payload = json.dumps(_jsonable(write.event.payload), sort_keys=True, separators=(",", ":"))
                    connection.execute(
                        f"INSERT INTO {events} VALUES (?,?,?,?,?,?,?)",
                        (write.event.event_id, write.event.aggregate_id, write.event.event_type, write.event.revision, write.event.schema_version, event_payload, write.event.occurred_at),
                    )
                connection.execute(
                    "INSERT INTO cross_domain_operations VALUES (?,?,?)",
                    (operation_id, fingerprint, str(self._clock())),
                )
            except Exception:
                connection.rollback()
                raise
            return CoordinationResult(operation_id, fingerprint, committed=True)


__all__ = ["SQLiteCrossDomainCoordinator"]
