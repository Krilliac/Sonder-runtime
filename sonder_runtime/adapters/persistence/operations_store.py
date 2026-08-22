"""Durable operations events, backup records, and maintenance locks.

SPEC-2 section 8.  ``operations.db`` is the audit spine of the production
runtime: security-sensitive actions, lifecycle transitions, backup runs,
and maintenance locks live here as low-cardinality event codes with
redacted detail.  Private prompts, raw workspace contents, credentials,
and memory text are prohibited — details carry identifiers, counts,
hashes, durations, and redacted paths only.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import sonder_runtime.adapters.persistence.migrations as sonder_migrations
from sonder_runtime.platform.logging import REDACTION_FAILED, Redactor

REDACTION_VERSION = 1

SEVERITIES = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@dataclass(frozen=True)
class OperationEvent:
    event_id: str
    occurred_at_utc: str
    component: str
    event_code: str
    severity: str
    summary: str
    detail: dict
    correlation_id: str | None = None
    principal_id: str | None = None
    operation_id: str | None = None


class MaintenanceLockHeld(RuntimeError):
    def __init__(self, lock_name: str, owner_id: str, reason: str) -> None:
        super().__init__(
            f"maintenance lock {lock_name!r} is held by {owner_id!r}: {reason}"
        )
        self.lock_name = lock_name
        self.owner_id = owner_id
        self.reason = reason


class OperationsStore:
    """Single writer interface over operations.db."""

    def __init__(
        self,
        db_path: str | None = None,
        *,
        redactor: Redactor | None = None,
        busy_timeout_ms: int = 5000,
    ) -> None:
        self._db_path = db_path or sonder_migrations.store_db_paths()["operations"]
        self._redactor = redactor or Redactor()
        self._busy_timeout_ms = busy_timeout_ms
        self._lock = threading.Lock()
        # The baseline migration owns the schema; opening the store never
        # creates tables ad-hoc.
        status = sonder_migrations.status("operations", self._db_path)
        if not status.current:
            sonder_migrations.migrate_store("operations", self._db_path)

    def _connect(self) -> sqlite3.Connection:
        return sonder_migrations.open_connection(
            self._db_path, busy_timeout_ms=self._busy_timeout_ms
        )

    # -- events ------------------------------------------------------------

    def record_event(
        self,
        *,
        component: str,
        event_code: str,
        severity: str = "INFO",
        summary: str,
        detail: dict | None = None,
        correlation_id: str | None = None,
        principal_id: str | None = None,
        operation_id: str | None = None,
    ) -> OperationEvent:
        if severity not in SEVERITIES:
            raise ValueError(f"invalid severity {severity!r}")
        try:
            detail_json = json.dumps(detail or {}, ensure_ascii=False, default=str)
            redacted = self._redactor.redact(detail_json)
            # A failed redaction replaces the whole detail payload.
            if redacted == REDACTION_FAILED:
                detail_json = json.dumps({"detail": REDACTION_FAILED})
            else:
                detail_json = redacted
                json.loads(detail_json)
        except (TypeError, ValueError):
            detail_json = json.dumps({"detail": REDACTION_FAILED})
        event = OperationEvent(
            event_id=f"evt_{uuid.uuid4().hex}",
            occurred_at_utc=_utc_now(),
            component=component,
            event_code=event_code,
            severity=severity,
            summary=self._redactor.redact(summary),
            detail=json.loads(detail_json),
            correlation_id=correlation_id,
            principal_id=principal_id,
            operation_id=operation_id,
        )
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "INSERT INTO operation_event (event_id, occurred_at_utc,"
                    " component, event_code, severity, correlation_id,"
                    " principal_id, operation_id, summary, detail_json,"
                    " redaction_version)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        event.event_id,
                        event.occurred_at_utc,
                        event.component,
                        event.event_code,
                        event.severity,
                        event.correlation_id,
                        event.principal_id,
                        event.operation_id,
                        event.summary,
                        json.dumps(event.detail, ensure_ascii=False),
                        REDACTION_VERSION,
                    ),
                )
            finally:
                conn.close()
        return event

    def recent_events(self, limit: int = 100) -> list[OperationEvent]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT event_id, occurred_at_utc, component, event_code,"
                " severity, correlation_id, principal_id, operation_id,"
                " summary, detail_json"
                " FROM operation_event ORDER BY occurred_at_utc DESC, event_id"
                " LIMIT ?",
                (max(1, min(limit, 1000)),),
            ).fetchall()
        finally:
            conn.close()
        return [
            OperationEvent(
                event_id=row[0],
                occurred_at_utc=row[1],
                component=row[2],
                event_code=row[3],
                severity=row[4],
                correlation_id=row[5],
                principal_id=row[6],
                operation_id=row[7],
                summary=row[8],
                detail=json.loads(row[9]),
            )
            for row in rows
        ]

    def prune_events(self, retention_days: int) -> int:
        cutoff = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ",
            time.gmtime(time.time() - retention_days * 86_400),
        )
        with self._lock:
            conn = self._connect()
            try:
                cursor = conn.execute(
                    "DELETE FROM operation_event WHERE occurred_at_utc < ?",
                    (cutoff,),
                )
                return cursor.rowcount
            finally:
                conn.close()

    # -- backup runs -------------------------------------------------------

    def start_backup_run(self, application_version: str) -> str:
        backup_id = f"bkp_{uuid.uuid4().hex}"
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "INSERT INTO backup_run (backup_id, started_at_utc, status,"
                    " application_version) VALUES (?, ?, 'running', ?)",
                    (backup_id, _utc_now(), application_version),
                )
            finally:
                conn.close()
        return backup_id

    def finish_backup_run(
        self,
        backup_id: str,
        *,
        status: str,
        manifest_path: str | None = None,
        total_bytes: int = 0,
        error_code: str | None = None,
    ) -> None:
        if status not in ("verified", "failed", "pruned"):
            raise ValueError(f"invalid terminal backup status {status!r}")
        with self._lock:
            conn = self._connect()
            try:
                cursor = conn.execute(
                    "UPDATE backup_run SET completed_at_utc = ?, status = ?,"
                    " manifest_path = ?, total_bytes = ?, error_code = ?"
                    " WHERE backup_id = ?",
                    (
                        _utc_now(),
                        status,
                        manifest_path,
                        total_bytes,
                        error_code,
                        backup_id,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ValueError(f"unknown backup run {backup_id!r}")
            finally:
                conn.close()

    def list_backup_runs(self, limit: int = 50) -> list[dict]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT backup_id, started_at_utc, completed_at_utc, status,"
                " application_version, manifest_path, total_bytes, error_code"
                " FROM backup_run ORDER BY started_at_utc DESC LIMIT ?",
                (max(1, min(limit, 500)),),
            ).fetchall()
        finally:
            conn.close()
        keys = (
            "backup_id",
            "started_at_utc",
            "completed_at_utc",
            "status",
            "application_version",
            "manifest_path",
            "total_bytes",
            "error_code",
        )
        return [dict(zip(keys, row)) for row in rows]

    # -- maintenance locks -------------------------------------------------

    def acquire_maintenance_lock(
        self,
        lock_name: str,
        *,
        owner_id: str,
        reason: str,
        ttl_seconds: int | None = None,
    ) -> None:
        """Take a named maintenance lock; expired locks are reclaimed."""
        expires = (
            time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + ttl_seconds)
            )
            if ttl_seconds
            else None
        )
        now = _utc_now()
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT owner_id, expires_at_utc, reason FROM maintenance_lock"
                    " WHERE lock_name = ?",
                    (lock_name,),
                ).fetchone()
                if row is not None:
                    holder, holder_expiry, holder_reason = row
                    if holder_expiry is None or holder_expiry > now:
                        conn.execute("ROLLBACK")
                        raise MaintenanceLockHeld(lock_name, holder, holder_reason)
                    conn.execute(
                        "DELETE FROM maintenance_lock WHERE lock_name = ?",
                        (lock_name,),
                    )
                conn.execute(
                    "INSERT INTO maintenance_lock (lock_name, owner_id,"
                    " acquired_at_utc, expires_at_utc, reason)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (lock_name, owner_id, now, expires, reason),
                )
                conn.execute("COMMIT")
            except MaintenanceLockHeld:
                raise
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.OperationalError:
                    pass
                raise
            finally:
                conn.close()

    def release_maintenance_lock(self, lock_name: str, *, owner_id: str) -> bool:
        with self._lock:
            conn = self._connect()
            try:
                cursor = conn.execute(
                    "DELETE FROM maintenance_lock WHERE lock_name = ?"
                    " AND owner_id = ?",
                    (lock_name, owner_id),
                )
                return cursor.rowcount == 1
            finally:
                conn.close()

    def active_maintenance_locks(self) -> list[dict]:
        now = _utc_now()
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT lock_name, owner_id, acquired_at_utc, expires_at_utc,"
                " reason FROM maintenance_lock"
                " WHERE expires_at_utc IS NULL OR expires_at_utc > ?",
                (now,),
            ).fetchall()
        finally:
            conn.close()
        keys = ("lock_name", "owner_id", "acquired_at_utc", "expires_at_utc", "reason")
        return [dict(zip(keys, row)) for row in rows]
