"""Domain-scoped SQLite adapter for the application outbox/CAS port."""
from __future__ import annotations

from collections.abc import Mapping
import json
from pathlib import Path
import re
import sqlite3
from threading import Lock
from typing import Any

from ....application.persistence.outbox_cas import (
    OutboxCASRepository, OutboxEvent, PersistenceError, TransactionNeutralRecord,
)


_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


def _jsonable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_jsonable(item) for item in value]
    return value


class SQLiteOutboxCASRepository(OutboxCASRepository):
    """One repository owner for one SQLite file and one table namespace."""

    def __init__(self, db_path: str | Path, *, namespace: str = "persistence") -> None:
        if not _IDENTIFIER.fullmatch(namespace):
            raise ValueError("namespace must be a lowercase identifier")
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._records = f"{namespace}_records"
        self._events = f"{namespace}_outbox_events"
        self._lock = Lock()
        with self._connect() as connection:
            connection.executescript(f"""
                CREATE TABLE IF NOT EXISTS {self._records} (
                    aggregate_id TEXT PRIMARY KEY,
                    revision INTEGER NOT NULL,
                    schema_version INTEGER NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS {self._events} (
                    event_id TEXT PRIMARY KEY,
                    aggregate_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    schema_version INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    occurred_at TEXT NOT NULL
                );
            """)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self._path), timeout=5.0)
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    @staticmethod
    def _record(row: tuple[Any, ...] | None) -> TransactionNeutralRecord | None:
        if row is None:
            return None
        aggregate_id, revision, schema_version, payload = row
        return TransactionNeutralRecord(aggregate_id, revision, json.loads(payload), schema_version=schema_version)

    @staticmethod
    def _event(row: tuple[Any, ...]) -> OutboxEvent:
        event_id, aggregate_id, event_type, revision, schema_version, payload, occurred_at = row
        return OutboxEvent(event_id, aggregate_id, event_type, revision, json.loads(payload), occurred_at, schema_version=schema_version)

    def get(self, aggregate_id: str) -> TransactionNeutralRecord | None:
        with self._connect() as connection:
            return self._record(connection.execute(
                f"SELECT aggregate_id,revision,schema_version,payload_json FROM {self._records} WHERE aggregate_id=?",
                (aggregate_id,),
            ).fetchone())

    def append(self, record: TransactionNeutralRecord, event: OutboxEvent, *, expected_revision: int) -> TransactionNeutralRecord | None:
        if record.aggregate_id != event.aggregate_id or record.revision != event.revision:
            raise PersistenceError("record and event identity/revision must match")
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._record(connection.execute(
                f"SELECT aggregate_id,revision,schema_version,payload_json FROM {self._records} WHERE aggregate_id=?",
                (record.aggregate_id,),
            ).fetchone())
            actual = -1 if current is None else current.revision
            if actual != expected_revision or record.revision != expected_revision + 1:
                connection.rollback()
                return None
            try:
                payload = json.dumps(_jsonable(record.payload), sort_keys=True, separators=(",", ":"))
                if current is None:
                    connection.execute(
                        f"INSERT INTO {self._records} VALUES (?,?,?,?)",
                        (record.aggregate_id, record.revision, record.schema_version, payload),
                    )
                else:
                    connection.execute(
                        f"UPDATE {self._records} SET revision=?,schema_version=?,payload_json=? WHERE aggregate_id=? AND revision=?",
                        (record.revision, record.schema_version, payload, record.aggregate_id, expected_revision),
                    )
                    if connection.total_changes != 1:
                        connection.rollback()
                        return None
                connection.execute(
                    f"INSERT INTO {self._events} VALUES (?,?,?,?,?,?,?)",
                    (event.event_id, event.aggregate_id, event.event_type, event.revision, event.schema_version,
                     json.dumps(_jsonable(event.payload), sort_keys=True, separators=(",", ":")), event.occurred_at),
                )
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise PersistenceError("CAS/outbox write violated an immutable identity") from exc
            return record

    def outbox(self) -> tuple[OutboxEvent, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT event_id,aggregate_id,event_type,revision,schema_version,payload_json,occurred_at FROM {self._events} ORDER BY rowid"
            ).fetchall()
        return tuple(self._event(row) for row in rows)


__all__ = ["SQLiteOutboxCASRepository"]
