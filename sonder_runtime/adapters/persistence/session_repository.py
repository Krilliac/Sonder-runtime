"""SQLite adapter for the append-only session event repository.

This store is deliberately independent from operations and memory storage.  A
per-session sequence and hash chain make ordering and accidental history edits
observable without exposing a mutable session projection.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path

from sonder_runtime.application.ports.session_repository import (
    IntegrityIssue,
    IntegrityReport,
    SessionEvent,
)

_DDL = """
CREATE TABLE IF NOT EXISTS session_event (
    session_id TEXT NOT NULL,
    sequence INTEGER NOT NULL CHECK (sequence > 0),
    event_id TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL,
    occurred_at_utc TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    previous_hash TEXT,
    event_hash TEXT NOT NULL,
    PRIMARY KEY (session_id, sequence)
);
CREATE INDEX IF NOT EXISTS ix_session_event_type
    ON session_event (session_id, event_type, sequence);
CREATE TRIGGER IF NOT EXISTS session_event_no_update
    BEFORE UPDATE ON session_event
    BEGIN SELECT RAISE(ABORT, 'session event history is append-only'); END;
CREATE TRIGGER IF NOT EXISTS session_event_no_delete
    BEFORE DELETE ON session_event
    BEGIN SELECT RAISE(ABORT, 'session event history is append-only'); END;
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _validate_bounds(start_sequence: int, end_sequence: int | None, limit: int, maximum: int) -> None:
    if not isinstance(start_sequence, int) or start_sequence < 1:
        raise ValueError("start_sequence must be a positive integer")
    if end_sequence is not None and (not isinstance(end_sequence, int) or end_sequence < start_sequence):
        raise ValueError("end_sequence must be >= start_sequence")
    if not isinstance(limit, int) or not 1 <= limit <= maximum:
        raise ValueError(f"limit must be between 1 and {maximum}")


class SQLiteSessionRepository:
    """Single-writer SQLite implementation of ``SessionRepository``."""

    def __init__(self, db_path: str | Path, *, max_read_limit: int = 10_000) -> None:
        if max_read_limit < 1:
            raise ValueError("max_read_limit must be positive")
        self._db_path = Path(db_path)
        self._max_read_limit = max_read_limit
        self._lock = threading.Lock()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_DDL)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path), timeout=5.0)
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    @staticmethod
    def _canonical_payload(payload: Mapping[str, object]) -> str:
        if not isinstance(payload, Mapping):
            raise TypeError("payload must be a mapping")
        try:
            return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            raise TypeError("payload must contain JSON-serializable values") from exc

    @staticmethod
    def _hash(session_id: str, sequence: int, event_id: str, event_type: str,
              occurred_at_utc: str, payload_json: str, previous_hash: str | None) -> str:
        material = json.dumps(
            [session_id, sequence, event_id, event_type, occurred_at_utc, payload_json, previous_hash],
            ensure_ascii=False, separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(material).hexdigest()

    @staticmethod
    def _row_to_event(row: tuple) -> SessionEvent:
        return SessionEvent(row[0], row[1], row[2], row[3], row[4], json.loads(row[5]), row[6], row[7])

    def append(self, session_id: str, event_type: str, payload: Mapping[str, object], *,
               event_id: str | None = None, occurred_at_utc: str | None = None) -> SessionEvent:
        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError("session_id must be non-empty")
        if not isinstance(event_type, str) or not event_type.strip():
            raise ValueError("event_type must be non-empty")
        payload_json = self._canonical_payload(payload)
        event_id = event_id or f"sev_{uuid.uuid4().hex}"
        occurred_at_utc = occurred_at_utc or _now()
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT sequence, event_hash FROM session_event WHERE session_id = ? ORDER BY sequence DESC LIMIT 1",
                (session_id,),
            ).fetchone()
            sequence = (row[0] + 1) if row else 1
            previous_hash = row[1] if row else None
            event_hash = self._hash(session_id, sequence, event_id, event_type, occurred_at_utc, payload_json, previous_hash)
            conn.execute(
                "INSERT INTO session_event VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (session_id, sequence, event_id, event_type, occurred_at_utc, payload_json, previous_hash, event_hash),
            )
        return SessionEvent(session_id, sequence, event_id, event_type, occurred_at_utc, json.loads(payload_json), previous_hash, event_hash)

    def read_range(self, session_id: str, *, start_sequence: int = 1,
                   end_sequence: int | None = None, limit: int = 1_000) -> tuple[SessionEvent, ...]:
        _validate_bounds(start_sequence, end_sequence, limit, self._max_read_limit)
        end = end_sequence if end_sequence is not None else 2**63 - 1
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT session_id, sequence, event_id, event_type, occurred_at_utc, payload_json, previous_hash, event_hash "
                "FROM session_event WHERE session_id = ? AND sequence BETWEEN ? AND ? ORDER BY sequence LIMIT ?",
                (session_id, start_sequence, end, limit),
            ).fetchall()
        return tuple(self._row_to_event(row) for row in rows)

    def search(self, *, session_id: str | None = None, event_type: str | None = None,
               text: str | None = None, limit: int | None = None) -> tuple[SessionEvent, ...]:
        limit = self._max_read_limit if limit is None else limit
        _validate_bounds(1, None, limit, self._max_read_limit)
        clauses, args = [], []
        if session_id is not None:
            clauses.append("session_id = ?"); args.append(session_id)
        if event_type is not None:
            clauses.append("event_type = ?"); args.append(event_type)
        if text is not None:
            clauses.append("payload_json LIKE ?"); args.append(f"%{text}%")
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT session_id, sequence, event_id, event_type, occurred_at_utc, payload_json, previous_hash, event_hash "
                f"FROM session_event{where} ORDER BY session_id, sequence LIMIT ?", (*args, limit),
            ).fetchall()
        return tuple(self._row_to_event(row) for row in rows)

    def export(self, session_id: str, *, start_sequence: int = 1,
               end_sequence: int | None = None, limit: int = 1_000) -> str:
        return "".join(json.dumps({"session_id": e.session_id, "sequence": e.sequence, "event_id": e.event_id,
                                    "event_type": e.event_type, "occurred_at_utc": e.occurred_at_utc,
                                    "payload": e.payload, "previous_hash": e.previous_hash, "event_hash": e.event_hash},
                                   ensure_ascii=False, sort_keys=True) + "\n"
                    for e in self.read_range(session_id, start_sequence=start_sequence, end_sequence=end_sequence, limit=limit))

    def inspect_integrity(self, session_id: str, *, start_sequence: int = 1,
                          end_sequence: int | None = None, limit: int = 10_000) -> IntegrityReport:
        events = self.read_range(session_id, start_sequence=start_sequence, end_sequence=end_sequence, limit=limit)
        issues: list[IntegrityIssue] = []
        expected = start_sequence
        previous_hash = None
        for event in events:
            if event.sequence != expected:
                issues.append(IntegrityIssue(event.sequence, "sequence_gap", f"expected {expected}"))
            if event.previous_hash != previous_hash:
                issues.append(IntegrityIssue(event.sequence, "previous_hash_mismatch", "hash chain predecessor differs"))
            calculated = self._hash(event.session_id, event.sequence, event.event_id, event.event_type,
                                    event.occurred_at_utc, self._canonical_payload(event.payload), event.previous_hash)
            if calculated != event.event_hash:
                issues.append(IntegrityIssue(event.sequence, "event_hash_mismatch", "event hash does not match content"))
            expected = event.sequence + 1
            previous_hash = event.event_hash
        return IntegrityReport(session_id, len(events), events[0].sequence if events else None,
                               events[-1].sequence if events else None, not issues, tuple(issues))
