"""SQLite implementation of the durable worker registry contract."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Mapping, Any

from sonder_runtime.adapters.persistence.owned_sqlite import transaction as owned_sqlite_transaction
from sonder_runtime.application.ports.worker_registry import (
    ACTIVE_WORKER_STATUSES, DuplicateWorkerError, WorkerLaunch, WorkerRecord,
    WorkerRegistryError, WorkerStatus,
)


_DDL = """
CREATE TABLE IF NOT EXISTS worker_registry (
    worker_id TEXT PRIMARY KEY, parent_id TEXT NOT NULL, role TEXT NOT NULL,
    model TEXT NOT NULL, backend TEXT NOT NULL, effort TEXT NOT NULL,
    scope_json TEXT NOT NULL, tools_json TEXT NOT NULL, budgets_json TEXT NOT NULL,
    retry_json TEXT NOT NULL, resume_key TEXT NOT NULL UNIQUE,
    idempotency_key TEXT NOT NULL UNIQUE, status TEXT NOT NULL,
    progress_json TEXT NOT NULL, verification_json TEXT NOT NULL,
    error TEXT NOT NULL, revision INTEGER NOT NULL, attempt_count INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_worker_registry_status ON worker_registry(status);
"""


def _json(value: Any) -> str:
    try:
        if isinstance(value, Mapping):
            value = {key: _json_value(item) for key, item in value.items()}
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
        if len(encoded.encode("ascii")) > 64 * 1024:
            raise WorkerRegistryError("worker registry JSON exceeds 64 KiB")
        return encoded
    except (TypeError, ValueError) as exc:
        raise WorkerRegistryError("worker registry state must be JSON-safe") from exc


def _json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    return value


class SQLiteWorkerRegistry:
    def __init__(self, db_path: str | Path) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with owned_sqlite_transaction(str(self._path), timeout=5.0) as connection:
            connection.executescript(_DDL)

    @staticmethod
    def _row(row: sqlite3.Row | tuple) -> WorkerRecord:
        values = dict(row) if isinstance(row, sqlite3.Row) else {
            "worker_id": row[0], "parent_id": row[1], "role": row[2], "model": row[3], "backend": row[4], "effort": row[5],
            "scope_json": row[6], "tools_json": row[7], "budgets_json": row[8], "retry_json": row[9], "resume_key": row[10],
            "idempotency_key": row[11], "status": row[12], "progress_json": row[13], "verification_json": row[14], "error": row[15], "revision": row[16], "attempt_count": row[17],
        }
        launch = WorkerLaunch(values["worker_id"], values["parent_id"], values["role"], values["model"], values["backend"], values["effort"],
                              tuple(json.loads(values["scope_json"])), tuple(json.loads(values["tools_json"])), json.loads(values["budgets_json"]), json.loads(values["retry_json"]), values["resume_key"], values["idempotency_key"])
        return WorkerRecord(launch, WorkerStatus(values["status"]), json.loads(values["progress_json"]), json.loads(values["verification_json"]), values["error"], int(values["revision"]), int(values["attempt_count"]))

    def get(self, worker_id: str) -> WorkerRecord | None:
        with owned_sqlite_transaction(str(self._path), timeout=5.0) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute("SELECT * FROM worker_registry WHERE worker_id=?", (worker_id,)).fetchone()
        return None if row is None else self._row(row)

    def admit(self, launch: WorkerLaunch) -> WorkerRecord:
        with owned_sqlite_transaction(str(self._path), timeout=5.0) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM worker_registry WHERE worker_id=? OR resume_key=? OR idempotency_key=?", (launch.worker_id, launch.resume_key, launch.idempotency_key)).fetchone()
            if row is not None:
                current = self._row(row)
                if current.status in ACTIVE_WORKER_STATUSES:
                    raise DuplicateWorkerError(f"worker launch already active: {launch.resume_key}")
                if current.launch != launch:
                    raise WorkerRegistryError("worker identity key is already bound to a different launch")
                return current
            connection.execute("INSERT INTO worker_registry VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                launch.worker_id, launch.parent_id, launch.role, launch.model, launch.backend, launch.effort,
                _json(launch.scope), _json(launch.allowed_tools), _json(launch.budgets), _json(launch.retry_policy),
                launch.resume_key, launch.idempotency_key, WorkerStatus.QUEUED.value, "{}", "{}", "", 0, 0,
            ))
            row = connection.execute("SELECT * FROM worker_registry WHERE worker_id=?", (launch.worker_id,)).fetchone()
        return self._row(row)

    def _update(self, worker_id: str, *, status: WorkerStatus | None = None, progress: Mapping[str, Any] | None = None, verification: Mapping[str, Any] | None = None, error: str | None = None, expected_revision: int, allowed: frozenset[WorkerStatus] = frozenset(), increment_attempt: bool = False) -> WorkerRecord | None:
        with owned_sqlite_transaction(str(self._path), timeout=5.0) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute("SELECT * FROM worker_registry WHERE worker_id=?", (worker_id,)).fetchone()
            if current is None or int(current["revision"]) != expected_revision:
                return None
            values = self._row(current)
            if values.status not in allowed:
                return None
            next_status = status or values.status
            attempt_count = values.attempt_count + (1 if increment_attempt else 0)
            max_attempts = int(values.launch.retry_policy.get("max_attempts", 1))
            if increment_attempt and attempt_count > max_attempts:
                raise WorkerRegistryError("worker retry budget exhausted")
            if error is not None and len(error) > 4096:
                raise WorkerRegistryError("worker error exceeds its bound")
            connection.execute("UPDATE worker_registry SET status=?,progress_json=?,verification_json=?,error=?,revision=revision+1,attempt_count=? WHERE worker_id=? AND revision=?", (
                next_status.value, _json(progress if progress is not None else values.progress), _json(verification if verification is not None else values.terminal_verification), error if error is not None else values.error, attempt_count, worker_id, expected_revision,
            ))
            row = connection.execute("SELECT * FROM worker_registry WHERE worker_id=?", (worker_id,)).fetchone()
        return self._row(row)

    def start(self, worker_id: str, *, expected_revision: int) -> WorkerRecord | None:
        return self._update(worker_id, status=WorkerStatus.RUNNING, expected_revision=expected_revision, allowed=frozenset((WorkerStatus.QUEUED, WorkerStatus.FAILED, WorkerStatus.INTERRUPTED)), increment_attempt=True)

    def progress(self, worker_id: str, progress: Mapping[str, Any], *, expected_revision: int) -> WorkerRecord | None:
        return self._update(worker_id, progress=progress, expected_revision=expected_revision, allowed=frozenset((WorkerStatus.RUNNING,)))

    def finish(self, worker_id: str, *, status: WorkerStatus, verification: Mapping[str, Any], error: str = "", expected_revision: int) -> WorkerRecord | None:
        if status not in {WorkerStatus.SUCCEEDED, WorkerStatus.FAILED, WorkerStatus.INTERRUPTED}:
            raise WorkerRegistryError("finish requires a terminal worker status")
        return self._update(worker_id, status=status, verification=verification, error=error, expected_revision=expected_revision, allowed=frozenset((WorkerStatus.RUNNING,)))


__all__ = ["SQLiteWorkerRegistry"]
