"""SQLite-backed durable job registry (JOB-002/003/004)."""
from __future__ import annotations

from sonder_runtime.adapters.persistence.owned_sqlite import connect as owned_sqlite_connect

from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
import errno
import hashlib
import json
from pathlib import Path
import sqlite3
from threading import Lock
from typing import Any, Callable, Iterator, Mapping
from uuid import uuid4

from sonder_runtime.application.execution.world_control import (
    BoundedOutputBuffer, OutputPage, OutputStream, OutputWatermark, SpillReference,
)
from sonder_runtime.application.jobs.durable_registry import (
    DurableJobView, JobRecoveryReport, ProcessTreeCleanupContract, ProcessTreeCleanupReceipt,
    ProcessTreeCleanupRequest,
)
from sonder_runtime.application.operations.startup_reconciliation import (
    DrainAction, DrainPlan, RecordKind, StartupObservation, build_drain_plan,
)
from sonder_runtime.application.ports.jobs import (
    JobClaim, JobCompletionReceipt, JobIdentity, JobReconciliationReport,
    JobRecord, JobStatus, MAX_JOB_ATTEMPTS, TERMINAL_JOB_STATUSES,
)
from sonder_runtime.domain.common.errors import (
    CapacityExceeded,
    ConcurrencyConflict,
    DependencyUnavailable,
    IntegrityFailure,
    SonderError,
)


from .worker_capacity import SQLiteWorkerCapacity, initialize_capacity_schema


_DDL = """
CREATE TABLE IF NOT EXISTS durable_process_cleanup (
 job_id TEXT PRIMARY KEY REFERENCES durable_job(job_id) ON DELETE CASCADE,
 evidence TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS durable_job (
    job_id TEXT PRIMARY KEY, kind TEXT NOT NULL, operation_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL, parent_job_id TEXT, parent_session_id TEXT,
    status TEXT NOT NULL, revision INTEGER NOT NULL, created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL, result_json TEXT, error TEXT NOT NULL,
    process_id INTEGER, process_group_id INTEGER, output_next INTEGER NOT NULL DEFAULT 1,
    output_dropped_before INTEGER NOT NULL DEFAULT 0,
    worker_id TEXT,
    lease_until TEXT,
    attempt INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    claim_token TEXT,
    metadata_json TEXT
);
CREATE INDEX IF NOT EXISTS durable_job_parent ON durable_job(parent_job_id);
CREATE INDEX IF NOT EXISTS durable_job_operation ON durable_job(operation_id);
CREATE TABLE IF NOT EXISTS durable_job_output (
    job_id TEXT NOT NULL, sequence INTEGER NOT NULL, stream TEXT NOT NULL,
    data TEXT NOT NULL, spill_json TEXT, PRIMARY KEY(job_id, sequence),
    FOREIGN KEY(job_id) REFERENCES durable_job(job_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS durable_job_receipt (
    job_id TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    receipt_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    payload_digest TEXT NOT NULL,
    committed_at TEXT NOT NULL,
    revision INTEGER NOT NULL,
    PRIMARY KEY(job_id, attempt),
    FOREIGN KEY(job_id) REFERENCES durable_job(job_id) ON DELETE CASCADE
);
"""


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _storage_failure(exc: BaseException) -> SonderError:
    """Map SQLite/OS failures without leaking paths or driver vocabulary."""
    message = str(exc).casefold()
    code = getattr(exc, "sqlite_errorcode", None)
    if isinstance(code, int):
        code &= 0xFF  # extended result codes retain the primary code here

    if code in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED} or any(
        marker in message
        for marker in (
            "database is locked",
            "database table is locked",
            "database is busy",
        )
    ):
        return ConcurrencyConflict("durable job storage is busy; retry the operation")
    if code == sqlite3.SQLITE_FULL or any(
        marker in message for marker in ("database or disk is full", "disk full", "no space left")
    ):
        return CapacityExceeded("durable job storage has no free capacity")
    if code in {sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB} or any(
        marker in message for marker in ("malformed", "not a database", "corrupt")
    ):
        return IntegrityFailure("durable job storage failed integrity validation")
    if isinstance(exc, OSError) and exc.errno in {
        errno.ENOSPC,
        getattr(errno, "EDQUOT", errno.ENOSPC),
    }:
        return CapacityExceeded("durable job storage has no free capacity")
    return DependencyUnavailable("durable job storage is unavailable")


class SQLiteDurableJobRegistry(SQLiteWorkerCapacity):
    """Durable implementation of the parent-linked job lifecycle.

    Each mutating operation is transaction-scoped.  Process termination is
    never performed here: recovery emits a bounded request to the injected
    platform supervisor and records interruption only after a complete receipt.
    """

    def __init__(self, db_path: str | Path, *, clock: Callable[[], str] = _now,
                 output_bounds: tuple[int, int] = (256, 64 * 1024),
                 connect_factory: Callable[..., sqlite3.Connection] | None = None) -> None:
        max_events, max_bytes = output_bounds
        if max_events < 1 or max_bytes < 1:
            raise ValueError("output bounds must be positive")
        self._path = Path(db_path)
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise _storage_failure(exc) from exc
        self._clock, self._max_events, self._max_bytes = clock, max_events, max_bytes
        if connect_factory is not None and not callable(connect_factory):
            raise TypeError("connect_factory must be callable")
        self._connect_factory = connect_factory or owned_sqlite_connect
        self._lock = Lock()
        with self._connect() as connection:
            initialize_schema(connection)
            initialize_capacity_schema(connection)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Open one bounded transaction and always release its file handle.

        The injected factory is deliberately narrow: production keeps stdlib
        SQLite while reliability tests can fail a specific statement without
        sleeps, filesystem quotas, or touching an operator database.
        """
        try:
            connection = self._connect_factory(str(self._path), timeout=5.0)
            with closing(connection):
                connection.execute("PRAGMA busy_timeout=5000")
                connection.execute("PRAGMA foreign_keys=ON")
                with connection:
                    yield connection
        except (sqlite3.Error, OSError) as exc:
            raise _storage_failure(exc) from exc

    @staticmethod
    def _record(row: tuple[Any, ...] | None) -> JobRecord | None:
        if row is None:
            return None
        job_id, kind, operation, idem, parent, parent_session, status, revision, created, updated, result, error, *_ = row
        identity = JobIdentity(job_id, kind, operation, idem, parent, parent_session)
        attempt = int(row[18]) if len(row) > 18 else 0
        max_attempts = int(row[19]) if len(row) > 19 else 3
        return JobRecord(
            identity, JobStatus(status), revision, created, updated,
            None if result is None else json.loads(result), error,
            attempt, max_attempts,
        )

    def _row(self, connection: sqlite3.Connection, job_id: str) -> tuple[Any, ...] | None:
        return connection.execute(
            "SELECT job_id,kind,operation_id,idempotency_key,parent_job_id,parent_session_id,status,"
            "revision,created_at,updated_at,result_json,error,process_id,process_group_id,output_next,"
            "output_dropped_before,worker_id,lease_until,attempt,max_attempts,claim_token "
            ",metadata_json "
            "FROM durable_job WHERE job_id=?", (job_id,)
        ).fetchone()

    def start(self, identity: JobIdentity, *, parent_job_id: str | None = None,
              process_id: int | None = None, process_group_id: int | None = None,
              max_attempts: int = 3, metadata: Mapping[str, Any] | None = None) -> JobRecord:
        if not isinstance(identity, JobIdentity):
            raise TypeError("identity must be a JobIdentity")
        parent = parent_job_id if parent_job_id is not None else identity.parent_job_id
        if parent == identity.job_id:
            raise ValueError("a job cannot be its own parent")
        if process_id is not None and (isinstance(process_id, bool) or process_id <= 0):
            raise ValueError("process_id must be positive")
        if process_group_id is not None and (isinstance(process_group_id, bool) or process_group_id <= 0):
            raise ValueError("process_group_id must be positive")
        if (isinstance(max_attempts, bool) or not isinstance(max_attempts, int)
                or not 1 <= max_attempts <= MAX_JOB_ATTEMPTS):
            raise ValueError(f"max_attempts must be between 1 and {MAX_JOB_ATTEMPTS}")
        if metadata is not None and not isinstance(metadata, Mapping):
            raise TypeError("metadata must be a mapping")
        if parent is not None and parent == identity.parent_job_id:
            pass
        elif parent is not None:
            identity = JobIdentity(identity.job_id, identity.kind, identity.operation_id,
                                   identity.idempotency_key, parent, identity.parent_session_id)
        now = self._clock()
        with self._lock, self._connect() as connection:
            if parent is not None and connection.execute(
                "SELECT 1 FROM durable_job WHERE job_id=?", (parent,)
            ).fetchone() is None:
                raise KeyError(f"parent job {parent!r} not found")
            try:
                connection.execute(
                    "INSERT INTO durable_job(job_id,kind,operation_id,idempotency_key,parent_job_id,parent_session_id,"
                    "status,revision,created_at,updated_at,result_json,error,process_id,process_group_id,max_attempts,metadata_json) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (identity.job_id, identity.kind, identity.operation_id, identity.idempotency_key,
                     identity.parent_job_id, identity.parent_session_id, JobStatus.PENDING.value, 0,
                     now, now, None, "", process_id, process_group_id, max_attempts,
                     _json(dict(metadata or {}))),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"job {identity.job_id!r} already exists") from exc
        return JobRecord(
            identity, JobStatus.PENDING, 0, now, now,
            attempt=0, max_attempts=max_attempts,
        )

    def _record_process_cleanup(self, job_id, proof):
        from ....application.jobs.durable_registry import _validate_cleanup_evidence

        encoded = _json(proof)
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            record = self._record(self._row(connection, job_id))
            if record is None:
                raise KeyError("job not found")
            _validate_cleanup_evidence(record, proof)
            prior = connection.execute(
                "SELECT evidence FROM durable_process_cleanup WHERE job_id=?", (job_id,)
            ).fetchone()
            if prior is not None:
                if prior[0] != encoded:
                    raise ValueError("immutable process cleanup evidence conflict")
                return
            connection.execute(
                "INSERT INTO durable_process_cleanup VALUES (?,?)", (job_id, encoded)
            )

    def process_cleanup_proof(self, job_id):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT evidence FROM durable_process_cleanup WHERE job_id=?", (job_id,)
            ).fetchone()
        return None if row is None else json.loads(row[0])

    def attach_process(
        self,
        job_id: str,
        *,
        process_id: int,
        process_group_id: int | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> JobRecord:
        """Atomically bind process identity to a reserved durable job."""
        if isinstance(process_id, bool) or process_id <= 0:
            raise ValueError("process_id must be positive")
        if process_group_id is not None and (
            isinstance(process_group_id, bool) or process_group_id <= 0
        ):
            raise ValueError("process_group_id must be positive")
        if metadata is not None and not isinstance(metadata, Mapping):
            raise TypeError("metadata must be a mapping")
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._row(connection, job_id)
            current = self._record(row)
            if current is None:
                raise KeyError(f"unknown job {job_id!r}")
            if current.is_terminal:
                raise ValueError("cannot attach a process to a terminal job")
            if row[12] is not None:
                raise ValueError("job already has an attached process")
            current_metadata = {} if row[21] is None else json.loads(row[21])
            current_metadata.update(dict(metadata or {}))
            now = self._clock()
            changed = connection.execute(
                "UPDATE durable_job SET process_id=?,process_group_id=?,metadata_json=?,"
                "revision=revision+1,updated_at=? WHERE job_id=? AND revision=? AND process_id IS NULL",
                (
                    process_id,
                    process_group_id,
                    _json(current_metadata),
                    now,
                    job_id,
                    current.revision,
                ),
            ).rowcount
            if changed != 1:
                raise ValueError("job process attachment conflicted")
            record = self._record(self._row(connection, job_id))
            assert record is not None
            return record

    def create(self, identity: JobIdentity, *, metadata: dict[str, Any] | None = None) -> JobRecord:
        """Satisfy the persistence-neutral JobRegistry creation port."""
        if metadata is not None and not isinstance(metadata, dict):
            raise TypeError("metadata must be a dict when provided")
        max_attempts = 3 if metadata is None else metadata.get("max_attempts", 3)
        return self.start(identity, max_attempts=max_attempts)

    def poll(self, job_id: str) -> JobRecord:
        with self._connect() as connection:
            record = self._record(self._row(connection, job_id))
        if record is None:
            raise KeyError(f"unknown job {job_id!r}")
        return record

    def get(self, job_id: str) -> JobRecord | None:
        with self._connect() as connection:
            return self._record(self._row(connection, job_id))

    def list(self, *, parent_job_id: str | None = None, include_terminal: bool = True,
             limit: int = 100) -> tuple[JobRecord, ...]:
        """Return a bounded durable listing, optionally scoped to one parent."""
        if isinstance(limit, bool) or limit < 1:
            raise ValueError("limit must be positive")
        clauses, args = [], []
        if parent_job_id is not None:
            clauses.append("parent_job_id=?")
            args.append(parent_job_id)
        if not include_terminal:
            clauses.append("status NOT IN (?,?,?)")
            args.extend(status.value for status in TERMINAL_JOB_STATUSES)
        if not clauses:
            clauses.append("1=1")
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT job_id,kind,operation_id,idempotency_key,parent_job_id,parent_session_id,status,"
                "revision,created_at,updated_at,result_json,error,process_id,process_group_id,output_next,"
                "output_dropped_before,worker_id,lease_until,attempt,max_attempts,claim_token "
                "FROM durable_job WHERE " + " AND ".join(clauses) +
                " ORDER BY rowid LIMIT ?", (*args, limit)
            ).fetchall()
        return tuple(self._record(row) for row in rows if row is not None)  # type: ignore[misc]

    def iter_kind(
        self,
        kind_prefix: str,
        *,
        include_terminal: bool = True,
        page_size: int = 256,
    ):
        """Page deterministically through every record matching a kind prefix."""
        if not isinstance(kind_prefix, str):
            raise TypeError("kind_prefix must be text")
        if isinstance(page_size, bool) or not 1 <= page_size <= 4096:
            raise ValueError("page_size must be within 1..4096")
        after_rowid = 0
        while True:
            clauses = ["rowid>?", "kind LIKE ? ESCAPE '\\'"]
            escaped = kind_prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            args: list[Any] = [after_rowid, escaped + "%"]
            if not include_terminal:
                clauses.append("status NOT IN (?,?,?)")
                args.extend(status.value for status in TERMINAL_JOB_STATUSES)
            with self._connect() as connection:
                rows = connection.execute(
                    "SELECT rowid,job_id,kind,operation_id,idempotency_key,parent_job_id,parent_session_id,status,"
                    "revision,created_at,updated_at,result_json,error,process_id,process_group_id,output_next,"
                    "output_dropped_before,worker_id,lease_until,attempt,max_attempts,claim_token "
                    "FROM durable_job WHERE " + " AND ".join(clauses) +
                    " ORDER BY rowid LIMIT ?",
                    (*args, page_size),
                ).fetchall()
            if not rows:
                return
            for row in rows:
                after_rowid = int(row[0])
                record = self._record(row[1:])
                if record is not None:
                    yield record
            if len(rows) < page_size:
                return

    def all(self, *, limit: int = 1000) -> tuple[JobRecord, ...]:
        if isinstance(limit, bool) or limit < 1:
            raise ValueError("limit must be positive")
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT job_id,kind,operation_id,idempotency_key,parent_job_id,parent_session_id,status,"
                "revision,created_at,updated_at,result_json,error,process_id,process_group_id,output_next,"
                "output_dropped_before,worker_id,lease_until,attempt,max_attempts,claim_token "
                "FROM durable_job ORDER BY rowid LIMIT ?", (limit,)
            ).fetchall()
        return tuple(self._record(row) for row in rows if row is not None)  # type: ignore[misc]

    def view(self, job_id: str) -> DurableJobView:
        with self._connect() as connection:
            row = self._row(connection, job_id)
            record = self._record(row)
            if record is None:
                raise KeyError(f"unknown job {job_id!r}")
            children = tuple(item[0] for item in connection.execute(
                "SELECT job_id FROM durable_job WHERE parent_job_id=? ORDER BY rowid", (job_id,)
            ).fetchall())
        metadata = {} if row[21] is None else json.loads(row[21])
        return DurableJobView(record, record.identity.parent_job_id, children,
                              row[12], row[13], metadata)

    def append_output(self, job_id: str, stream: OutputStream, data: str,
                      *, spill: SpillReference | None = None) -> None:
        if not isinstance(stream, OutputStream) or not isinstance(data, str):
            raise TypeError("stream and data are required")
        with self._lock, self._connect() as connection:
            row = self._row(connection, job_id)
            if row is None:
                raise KeyError(f"unknown job {job_id!r}")
            sequence = row[14]
            connection.execute(
                "INSERT INTO durable_job_output(job_id,sequence,stream,data,spill_json) VALUES (?,?,?,?,?)",
                (job_id, sequence, stream.value, data, None if spill is None else _json({
                    "digest": spill.digest, "preview": spill.preview, "size": spill.size,
                    "mime_type": spill.mime_type, "owner_id": spill.owner_id,
                })),
            )
            connection.execute("UPDATE durable_job SET output_next=? WHERE job_id=?", (sequence + 1, job_id))
            rows = connection.execute(
                "SELECT sequence,data FROM durable_job_output WHERE job_id=? ORDER BY sequence", (job_id,)
            ).fetchall()
            total = sum(len(item[1].encode("utf-8")) for item in rows)
            while len(rows) > self._max_events or total > self._max_bytes:
                old = rows.pop(0)
                total -= len(old[1].encode("utf-8"))
                connection.execute("DELETE FROM durable_job_output WHERE job_id=? AND sequence=?", (job_id, old[0]))
                connection.execute("UPDATE durable_job SET output_dropped_before=? WHERE job_id=?", (old[0], job_id))

    def stream(self, job_id: str, *, after: OutputWatermark | None = None,
               max_events: int = 64, max_bytes: int = 16 * 1024) -> OutputPage:
        if isinstance(max_events, bool) or max_events < 1 or isinstance(max_bytes, bool) or max_bytes < 1:
            raise ValueError("output read bounds must be positive")
        cursor = after or OutputWatermark(0)
        with self._connect() as connection:
            row = self._row(connection, job_id)
            if row is None:
                raise KeyError(f"unknown job {job_id!r}")
            events = connection.execute(
                "SELECT sequence,stream,data,spill_json FROM durable_job_output WHERE job_id=? AND sequence>? ORDER BY sequence",
                (job_id, cursor.sequence),
            ).fetchall()
            selected = []
            used = 0
            for sequence, stream, data, spill_raw in events:
                size = len(data.encode("utf-8"))
                if selected and (len(selected) >= max_events or used + size > max_bytes):
                    break
                spill = None
                if spill_raw is not None:
                    value = json.loads(spill_raw)
                    spill = SpillReference(value["digest"], value["preview"], value["size"], value["mime_type"], value["owner_id"])
                selected.append((sequence, OutputStream(stream), data, spill))
                used += size
                if len(selected) >= max_events or used >= max_bytes:
                    break
            last = OutputWatermark(selected[-1][0] if selected else cursor.sequence)
            has_more = any(item[0] > last.sequence for item in events)
            truncated = cursor.sequence < row[15]
        from sonder_runtime.application.execution.world_control import OutputEvent
        return OutputPage(tuple(OutputEvent(OutputWatermark(seq), stream, data, spill)
                                for seq, stream, data, spill in selected), last, has_more, truncated)

    def transition(self, job_id: str, status: JobStatus, *, result: Any = None, error: str = "") -> JobRecord:
        if not isinstance(status, JobStatus):
            raise TypeError("status must be a JobStatus")
        with self._lock, self._connect() as connection:
            row = self._row(connection, job_id)
            current = self._record(row)
            if current is None:
                raise KeyError(f"unknown job {job_id!r}")
            if current.is_terminal:
                return current
            if status is JobStatus.SUCCEEDED and error:
                raise ValueError("successful jobs cannot carry an error")
            now = self._clock()
            connection.execute(
                "UPDATE durable_job SET status=?,revision=?,updated_at=?,result_json=?,error=? WHERE job_id=? AND revision=?",
                (status.value, current.revision + 1, now, None if result is None else _json(result), error,
                 job_id, current.revision),
            )
            return self._record(self._row(connection, job_id))  # type: ignore[return-value]

    @staticmethod
    def _lease_until(now: str, lease_seconds: int) -> str:
        value = datetime.fromisoformat(now.replace("Z", "+00:00"))
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return (value + timedelta(seconds=lease_seconds)).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _lease_active(value: str | None, now: str) -> bool:
        if not value:
            return False
        current = datetime.fromisoformat(now.replace("Z", "+00:00"))
        expiry = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        return expiry > current

    def claim(self, job_id: str, worker_id: str, *, lease_seconds: int = 300) -> JobClaim | None:
        """Durably claim a non-terminal job for one worker."""
        if not isinstance(worker_id, str) or not worker_id.strip():
            raise ValueError("worker_id must be non-empty")
        if isinstance(lease_seconds, bool) or lease_seconds < 1:
            raise ValueError("lease_seconds must be positive")
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._row(connection, job_id)
            current = self._record(row)
            if current is None:
                return None
            now = self._clock()
            if current.is_terminal or current.attempt >= current.max_attempts:
                return None
            if row[16] and self._lease_active(row[17], now):
                return None
            until = self._lease_until(now, lease_seconds)
            claim_token = uuid4().hex
            attempt = current.attempt + 1
            changed = connection.execute(
                "UPDATE durable_job SET status=?,revision=?,updated_at=?,worker_id=?,lease_until=?,"
                "attempt=?,claim_token=? "
                "WHERE job_id=? AND revision=?",
                (JobStatus.CLAIMED.value, current.revision + 1, now, worker_id, until,
                 attempt, claim_token,
                 job_id, current.revision),
            ).rowcount
            return JobClaim(
                job_id, worker_id, until, current.revision + 1,
                claim_token, attempt,
            ) if changed == 1 else None

    def heartbeat(self, job_id: str, worker_id: str, *, lease_seconds: int = 300,
                  claim_token: str | None = None) -> bool:
        if not isinstance(worker_id, str) or not worker_id.strip():
            raise ValueError("worker_id must be non-empty")
        if isinstance(lease_seconds, bool) or lease_seconds < 1:
            raise ValueError("lease_seconds must be positive")
        if claim_token is not None and (not isinstance(claim_token, str) or not claim_token.strip()):
            raise ValueError("claim_token must be non-empty when provided")
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._row(connection, job_id)
            current = self._record(row)
            if current is None or current.is_terminal:
                return False
            now = self._clock()
            if row[16] != worker_id or not self._lease_active(row[17], now):
                return False
            if claim_token is not None and row[20] != claim_token:
                return False
            until = self._lease_until(now, lease_seconds)
            changed = connection.execute(
                "UPDATE durable_job SET status=?,revision=?,updated_at=?,lease_until=? "
                "WHERE job_id=? AND revision=? AND worker_id=?",
                (JobStatus.RUNNING.value, current.revision + 1, now, until,
                 job_id, current.revision, worker_id),
            ).rowcount
            return changed == 1

    def finish(self, job_id: str, worker_id: str, status: JobStatus, *, result: Any = None,
               error: str = "", claim_token: str | None = None) -> JobRecord | None:
        if not isinstance(status, JobStatus):
            raise TypeError("status must be a JobStatus")
        if status not in TERMINAL_JOB_STATUSES:
            raise ValueError("finish status must be terminal")
        if not isinstance(worker_id, str) or not worker_id.strip():
            raise ValueError("worker_id must be non-empty")
        if claim_token is not None and (not isinstance(claim_token, str) or not claim_token.strip()):
            raise ValueError("claim_token must be non-empty when provided")
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._row(connection, job_id)
            current = self._record(row)
            if current is None or current.is_terminal:
                return current
            now = self._clock()
            if row[16] != worker_id or not self._lease_active(row[17], now):
                return None
            if claim_token is not None and row[20] != claim_token:
                return None
            if status is JobStatus.SUCCEEDED and error:
                raise ValueError("successful jobs cannot carry an error")
            changed = connection.execute(
                "UPDATE durable_job SET status=?,revision=?,updated_at=?,result_json=?,error=?,"
                "worker_id=NULL,lease_until=NULL,claim_token=NULL "
                "WHERE job_id=? AND revision=? AND worker_id=?",
                (status.value, current.revision + 1, now,
                 None if result is None else _json(result), error,
                 job_id, current.revision, worker_id),
            ).rowcount
            return self._record(self._row(connection, job_id)) if changed == 1 else None

    @staticmethod
    def _receipt(row: tuple[Any, ...] | None) -> JobCompletionReceipt | None:
        if row is None:
            return None
        job_id, attempt, receipt_key, status, digest, committed_at, revision = row
        return JobCompletionReceipt(
            str(job_id), int(attempt), str(receipt_key), JobStatus(str(status)),
            str(digest), str(committed_at), int(revision),
        )

    @staticmethod
    def _completion_digest(status: JobStatus, result: Any, error: str) -> str:
        payload = _json({"status": status.value, "result": result, "error": error})
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def completion_receipt(
        self, job_id: str, *, attempt: int | None = None,
    ) -> JobCompletionReceipt | None:
        if attempt is not None and (isinstance(attempt, bool) or attempt < 1):
            raise ValueError("attempt must be positive when provided")
        with self._connect() as connection:
            if attempt is None:
                row = connection.execute(
                    "SELECT job_id,attempt,receipt_key,status,payload_digest,committed_at,revision "
                    "FROM durable_job_receipt WHERE job_id=? ORDER BY attempt DESC LIMIT 1",
                    (job_id,),
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT job_id,attempt,receipt_key,status,payload_digest,committed_at,revision "
                    "FROM durable_job_receipt WHERE job_id=? AND attempt=?",
                    (job_id, attempt),
                ).fetchone()
        return self._receipt(row)

    def finish_once(
        self,
        job_id: str,
        worker_id: str,
        status: JobStatus,
        *,
        receipt_key: str,
        result: Any = None,
        error: str = "",
        claim_token: str | None = None,
    ) -> JobCompletionReceipt | None:
        """Atomically commit one terminal result and its per-attempt receipt.

        Replaying the same receipt key and payload returns the stored receipt.
        A conflicting key or payload is rejected without changing job state.
        """
        if status not in TERMINAL_JOB_STATUSES:
            raise ValueError("finish status must be terminal")
        if not isinstance(worker_id, str) or not worker_id.strip():
            raise ValueError("worker_id must be non-empty")
        if not isinstance(receipt_key, str) or not receipt_key.strip():
            raise ValueError("receipt_key must be non-empty")
        if claim_token is not None and (not isinstance(claim_token, str) or not claim_token.strip()):
            raise ValueError("claim_token must be non-empty when provided")
        if status is JobStatus.SUCCEEDED and error:
            raise ValueError("successful jobs cannot carry an error")
        digest = self._completion_digest(status, result, error)
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._row(connection, job_id)
            current = self._record(row)
            if current is None:
                return None
            replay_row = connection.execute(
                "SELECT job_id,attempt,receipt_key,status,payload_digest,committed_at,revision "
                "FROM durable_job_receipt WHERE receipt_key=?",
                (receipt_key,),
            ).fetchone()
            replay = self._receipt(replay_row)
            if replay is not None:
                if (replay.job_id, replay.status, replay.payload_digest) != (
                    job_id, status, digest,
                ):
                    raise ValueError("completion receipt key is already committed")
                return replay
            existing_row = connection.execute(
                "SELECT job_id,attempt,receipt_key,status,payload_digest,committed_at,revision "
                "FROM durable_job_receipt WHERE job_id=? AND attempt=?",
                (job_id, current.attempt),
            ).fetchone()
            existing = self._receipt(existing_row)
            if existing is not None:
                if (existing.receipt_key, existing.status, existing.payload_digest) != (
                    receipt_key, status, digest,
                ):
                    raise ValueError("completion receipt conflicts with committed attempt")
                return existing
            if current.is_terminal:
                raise ValueError("terminal job has no matching completion receipt")
            now = self._clock()
            if row[16] != worker_id or not self._lease_active(row[17], now):
                return None
            if claim_token is not None and row[20] != claim_token:
                return None
            revision = current.revision + 1
            changed = connection.execute(
                "UPDATE durable_job SET status=?,revision=?,updated_at=?,result_json=?,error=?,"
                "worker_id=NULL,lease_until=NULL,claim_token=NULL "
                "WHERE job_id=? AND revision=? AND worker_id=?",
                (status.value, revision, now, None if result is None else _json(result),
                 error, job_id, current.revision, worker_id),
            ).rowcount
            if changed != 1:
                return None
            try:
                connection.execute(
                    "INSERT INTO durable_job_receipt(job_id,attempt,receipt_key,status,"
                    "payload_digest,committed_at,revision) VALUES (?,?,?,?,?,?,?)",
                    (job_id, current.attempt, receipt_key, status.value, digest, now, revision),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("completion receipt key is already committed") from exc
            return JobCompletionReceipt(
                job_id, current.attempt, receipt_key, status, digest, now, revision,
            )

    def retry(self, job_id: str, *, expected_revision: int | None = None) -> JobRecord | None:
        """Explicitly requeue a failed/interrupted job within its persisted budget."""
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._row(connection, job_id)
            current = self._record(row)
            if current is None:
                return None
            if expected_revision is not None and current.revision != expected_revision:
                return None
            if current.status not in {JobStatus.FAILED, JobStatus.INTERRUPTED}:
                raise ValueError("only failed or interrupted jobs can be retried")
            if current.attempt >= current.max_attempts:
                raise ValueError("job retry budget is exhausted")
            now = self._clock()
            changed = connection.execute(
                "UPDATE durable_job SET status=?,revision=revision+1,updated_at=?,"
                "result_json=NULL,error='',worker_id=NULL,lease_until=NULL,claim_token=NULL "
                "WHERE job_id=? AND revision=?",
                (JobStatus.PENDING.value, now, job_id, current.revision),
            ).rowcount
            return self._record(self._row(connection, job_id)) if changed == 1 else None

    def cancel(
        self,
        job_id: str,
        *,
        reason: str = "cancelled",
        max_descendants: int = 256,
    ) -> tuple[JobRecord, ...]:
        if not reason.strip():
            raise ValueError("cancellation reason is required")
        if isinstance(max_descendants, bool) or max_descendants < 0:
            raise ValueError("max_descendants must be a non-negative integer")
        queue = [job_id]
        ids: list[str] = []
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            while queue:
                current = queue.pop(0)
                if current in ids:
                    continue
                if self._row(connection, current) is None:
                    raise KeyError(f"unknown job {current!r}")
                ids.append(current)
                if len(ids) - 1 > max_descendants:
                    raise ValueError("job cancellation exceeds max_descendants")
                queue.extend(item[0] for item in connection.execute(
                    "SELECT job_id FROM durable_job WHERE parent_job_id=? ORDER BY rowid", (current,)
                ).fetchall())
            now = self._clock()
            for current in ids:
                connection.execute(
                    "UPDATE durable_job SET status=?,revision=revision+1,updated_at=?,error=?,"
                    "worker_id=NULL,lease_until=NULL,claim_token=NULL "
                    "WHERE job_id=? AND status NOT IN (?,?,?)",
                    (JobStatus.CANCELLED.value, now, reason, current,
                     JobStatus.SUCCEEDED.value, JobStatus.FAILED.value, JobStatus.CANCELLED.value),
                )
            records = tuple(self._record(self._row(connection, item)) for item in ids)
            return tuple(record for record in records if record is not None)

    def request_cancellation(
        self,
        job_id: str,
        *,
        reason: str = "cancelled",
        max_descendants: int = 256,
    ) -> tuple[JobRecord, ...]:
        """Persist cancellation intent without claiming process quiescence."""
        if not reason.strip():
            raise ValueError("cancellation reason is required")
        if isinstance(max_descendants, bool) or max_descendants < 0:
            raise ValueError("max_descendants must be a non-negative integer")
        queue = [job_id]
        ids: list[str] = []
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            while queue:
                current = queue.pop(0)
                if current in ids:
                    continue
                if self._row(connection, current) is None:
                    raise KeyError(f"unknown job {current!r}")
                ids.append(current)
                if len(ids) - 1 > max_descendants:
                    raise ValueError("job cancellation exceeds max_descendants")
                queue.extend(item[0] for item in connection.execute(
                    "SELECT job_id FROM durable_job WHERE parent_job_id=? ORDER BY rowid",
                    (current,),
                ).fetchall())
            now = self._clock()
            for current in ids:
                connection.execute(
                    "UPDATE durable_job SET status=?,revision=revision+1,updated_at=?,error=? "
                    "WHERE job_id=? AND status NOT IN (?,?,?,?)",
                    (
                        JobStatus.CANCELLATION_REQUESTED.value,
                        now,
                        reason,
                        current,
                        JobStatus.SUCCEEDED.value,
                        JobStatus.FAILED.value,
                        JobStatus.CANCELLED.value,
                        JobStatus.CANCELLATION_REQUESTED.value,
                    ),
                )
            records = tuple(self._record(self._row(connection, item)) for item in ids)
            return tuple(record for record in records if record is not None)

    def collect(self, job_id: str) -> JobRecord:
        record = self.poll(job_id)
        if not record.is_terminal:
            raise ValueError("job is not terminal")
        return record

    def reconcile_stale(
        self, *, now: str | None = None, max_records: int = 100,
    ) -> JobReconciliationReport:
        """Reconcile one bounded page of expired leases with stable diagnostics."""
        if isinstance(max_records, bool) or max_records < 1:
            raise ValueError("max_records must be positive")
        current_time = self._clock() if now is None else now
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT job_id,revision FROM durable_job WHERE status IN (?,?) "
                "AND lease_until IS NOT NULL AND lease_until<=? "
                "ORDER BY lease_until,job_id LIMIT ?",
                (JobStatus.CLAIMED.value, JobStatus.RUNNING.value,
                 current_time, max_records + 1),
            ).fetchall()
            selected = rows[:max_records]
            interrupted: list[str] = []
            for job_id, revision in selected:
                changed = connection.execute(
                    "UPDATE durable_job SET status=?,revision=revision+1,updated_at=?,"
                    "worker_id=NULL,lease_until=NULL,claim_token=NULL "
                    "WHERE job_id=? AND revision=? AND status IN (?,?) AND lease_until<=?",
                    (JobStatus.INTERRUPTED.value, current_time, job_id, revision,
                     JobStatus.CLAIMED.value, JobStatus.RUNNING.value, current_time),
                ).rowcount
                if changed == 1:
                    interrupted.append(str(job_id))
        return JobReconciliationReport(
            len(selected), tuple(interrupted), truncated=len(rows) > max_records,
        )

    def reconcile(self, *, now: str | None = None, max_records: int = 100) -> int:
        """Compatibility projection of bounded stale-lease reconciliation."""
        return self.reconcile_stale(now=now, max_records=max_records).interrupted

    def reconcile_recovery(self, *, owner_instance_id: str = "", owner_alive: bool | None = None,
                           max_records: int = 100, max_process_descendants: int = 64) -> DrainPlan:
        """Build the bounded process-tree recovery plan for startup."""
        observations = []
        for record in self.all(limit=max_records + 1):
            view = self.view(record.identity.job_id)
            observations.append(StartupObservation(
                RecordKind.JOB, record.identity.job_id, record.status.value,
                owner_instance_id=owner_instance_id, owner_alive=owner_alive,
                checkpoint_available=record.status in {JobStatus.PAUSED, JobStatus.INTERRUPTED},
                retryable=record.status in {JobStatus.PENDING, JobStatus.PAUSED, JobStatus.INTERRUPTED},
                process_id=view.process_id, process_group_id=view.process_group_id,
            ))
        plan = build_drain_plan(observations, max_records=max_records,
                                max_process_descendants=max_process_descendants)
        for item in plan.results:
            if item.action is DrainAction.MARK_INTERRUPTED:
                record = self.poll(item.observation.record_id)
                if not record.is_terminal and record.status is not JobStatus.INTERRUPTED:
                    self.transition(record.identity.job_id, JobStatus.INTERRUPTED)
        return plan

    def reconcile_with_cleanup(self, supervisor: ProcessTreeCleanupContract, **kwargs: Any) -> JobRecoveryReport:
        plan = self.reconcile_recovery(**kwargs)
        receipts: list[ProcessTreeCleanupReceipt] = []
        interrupted: list[str] = []
        for intent in plan.cleanup_intents:
            request = ProcessTreeCleanupRequest(intent.record_id, intent.process_id,
                                                intent.process_group_id, intent.max_descendants, intent.reason)
            receipt = supervisor.cleanup(request)
            if not isinstance(receipt, ProcessTreeCleanupReceipt):
                raise TypeError("process supervisor returned an invalid receipt")
            if receipt.job_id != intent.record_id:
                raise ValueError("process supervisor returned a receipt for the wrong job")
            receipts.append(receipt)
            if receipt.complete:
                record = self.poll(intent.record_id)
                if not record.is_terminal:
                    self.transition(intent.record_id, JobStatus.INTERRUPTED, error="orphan process tree cleaned")
                    interrupted.append(intent.record_id)
        return JobRecoveryReport(plan, tuple(receipts), tuple(interrupted))


def initialize_schema(connection: sqlite3.Connection) -> None:
    """Create or upgrade the durable-job schema on an existing connection."""
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(_DDL)
    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(durable_job)")
    }
    if "worker_id" not in columns:
        connection.execute("ALTER TABLE durable_job ADD COLUMN worker_id TEXT")
    if "lease_until" not in columns:
        connection.execute("ALTER TABLE durable_job ADD COLUMN lease_until TEXT")
    if "attempt" not in columns:
        connection.execute(
            "ALTER TABLE durable_job ADD COLUMN attempt INTEGER NOT NULL DEFAULT 0"
        )
    if "max_attempts" not in columns:
        connection.execute(
            "ALTER TABLE durable_job ADD COLUMN max_attempts INTEGER NOT NULL DEFAULT 3"
        )
    if "claim_token" not in columns:
        connection.execute("ALTER TABLE durable_job ADD COLUMN claim_token TEXT")
    if "metadata_json" not in columns:
        connection.execute("ALTER TABLE durable_job ADD COLUMN metadata_json TEXT")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS durable_job_stale_lease "
        "ON durable_job(status, lease_until)"
    )


__all__ = ["JobRecoveryReport", "SQLiteDurableJobRegistry", "initialize_schema"]
