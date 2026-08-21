"""SQLite-backed durable job registry (JOB-002/003/004)."""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
from threading import Lock
from typing import Any, Callable
from datetime import datetime, timedelta, timezone

from sonder_runtime.application.execution.world_control import (
    BoundedOutputBuffer, OutputPage, OutputStream, OutputWatermark, SpillReference,
)
from sonder_runtime.application.jobs.durable_registry import (
    DurableJobView, ProcessTreeCleanupContract, ProcessTreeCleanupReceipt,
    ProcessTreeCleanupRequest,
)
from sonder_runtime.application.operations.startup_reconciliation import (
    DrainAction, DrainPlan, RecordKind, StartupObservation, build_drain_plan,
)
from sonder_runtime.application.ports.jobs import (
    JobClaim, JobIdentity, JobRecord, JobStatus, TERMINAL_JOB_STATUSES,
)


_DDL = """
CREATE TABLE IF NOT EXISTS durable_job (
    job_id TEXT PRIMARY KEY, kind TEXT NOT NULL, operation_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL, parent_job_id TEXT, parent_session_id TEXT,
    status TEXT NOT NULL, revision INTEGER NOT NULL, created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL, result_json TEXT, error TEXT NOT NULL,
    process_id INTEGER, process_group_id INTEGER, output_next INTEGER NOT NULL DEFAULT 1,
    output_dropped_before INTEGER NOT NULL DEFAULT 0,
    worker_id TEXT,
    lease_until TEXT
);
CREATE INDEX IF NOT EXISTS durable_job_parent ON durable_job(parent_job_id);
CREATE INDEX IF NOT EXISTS durable_job_operation ON durable_job(operation_id);
CREATE TABLE IF NOT EXISTS durable_job_output (
    job_id TEXT NOT NULL, sequence INTEGER NOT NULL, stream TEXT NOT NULL,
    data TEXT NOT NULL, spill_json TEXT, PRIMARY KEY(job_id, sequence),
    FOREIGN KEY(job_id) REFERENCES durable_job(job_id) ON DELETE CASCADE
);
"""


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True, slots=True)
class JobRecoveryReport:
    plan: DrainPlan
    cleanup_receipts: tuple[ProcessTreeCleanupReceipt, ...]
    interrupted_job_ids: tuple[str, ...]


class SQLiteDurableJobRegistry:
    """Durable implementation of the parent-linked job lifecycle.

    Each mutating operation is transaction-scoped.  Process termination is
    never performed here: recovery emits a bounded request to the injected
    platform supervisor and records interruption only after a complete receipt.
    """

    def __init__(self, db_path: str | Path, *, clock: Callable[[], str] = _now,
                 output_bounds: tuple[int, int] = (256, 64 * 1024)) -> None:
        max_events, max_bytes = output_bounds
        if max_events < 1 or max_bytes < 1:
            raise ValueError("output bounds must be positive")
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._clock, self._max_events, self._max_bytes = clock, max_events, max_bytes
        self._lock = Lock()
        with self._connect() as connection:
            initialize_schema(connection)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self._path), timeout=5.0)
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @staticmethod
    def _record(row: tuple[Any, ...] | None) -> JobRecord | None:
        if row is None:
            return None
        job_id, kind, operation, idem, parent, parent_session, status, revision, created, updated, result, error, *_ = row
        identity = JobIdentity(job_id, kind, operation, idem, parent, parent_session)
        return JobRecord(identity, JobStatus(status), revision, created, updated,
                         None if result is None else json.loads(result), error)

    def _row(self, connection: sqlite3.Connection, job_id: str) -> tuple[Any, ...] | None:
        return connection.execute(
            "SELECT job_id,kind,operation_id,idempotency_key,parent_job_id,parent_session_id,status,"
            "revision,created_at,updated_at,result_json,error,process_id,process_group_id,output_next,"
            "output_dropped_before,worker_id,lease_until FROM durable_job WHERE job_id=?", (job_id,)
        ).fetchone()

    def start(self, identity: JobIdentity, *, parent_job_id: str | None = None,
              process_id: int | None = None, process_group_id: int | None = None) -> JobRecord:
        if not isinstance(identity, JobIdentity):
            raise TypeError("identity must be a JobIdentity")
        parent = parent_job_id if parent_job_id is not None else identity.parent_job_id
        if parent == identity.job_id:
            raise ValueError("a job cannot be its own parent")
        if process_id is not None and (isinstance(process_id, bool) or process_id <= 0):
            raise ValueError("process_id must be positive")
        if process_group_id is not None and (isinstance(process_group_id, bool) or process_group_id <= 0):
            raise ValueError("process_group_id must be positive")
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
                    "status,revision,created_at,updated_at,result_json,error,process_id,process_group_id) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (identity.job_id, identity.kind, identity.operation_id, identity.idempotency_key,
                     identity.parent_job_id, identity.parent_session_id, JobStatus.PENDING.value, 0,
                     now, now, None, "", process_id, process_group_id),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"job {identity.job_id!r} already exists") from exc
        return JobRecord(identity, JobStatus.PENDING, 0, now, now)

    def create(self, identity: JobIdentity, *, metadata: dict[str, Any] | None = None) -> JobRecord:
        """Satisfy the persistence-neutral JobRegistry creation port."""
        if metadata is not None and not isinstance(metadata, dict):
            raise TypeError("metadata must be a dict when provided")
        return self.start(identity)

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
                "output_dropped_before,worker_id,lease_until FROM durable_job WHERE " + " AND ".join(clauses) +
                " ORDER BY rowid LIMIT ?", (*args, limit)
            ).fetchall()
        return tuple(self._record(row) for row in rows if row is not None)  # type: ignore[misc]

    def all(self, *, limit: int = 1000) -> tuple[JobRecord, ...]:
        if isinstance(limit, bool) or limit < 1:
            raise ValueError("limit must be positive")
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT job_id,kind,operation_id,idempotency_key,parent_job_id,parent_session_id,status,"
                "revision,created_at,updated_at,result_json,error,process_id,process_group_id,output_next,"
                "output_dropped_before,worker_id,lease_until FROM durable_job ORDER BY rowid LIMIT ?", (limit,)
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
        return DurableJobView(record, record.identity.parent_job_id, children,
                              row[12], row[13])

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
            if current.is_terminal or (row[16] and self._lease_active(row[17], now)):
                return None
            until = self._lease_until(now, lease_seconds)
            changed = connection.execute(
                "UPDATE durable_job SET status=?,revision=?,updated_at=?,worker_id=?,lease_until=? "
                "WHERE job_id=? AND revision=?",
                (JobStatus.CLAIMED.value, current.revision + 1, now, worker_id, until,
                 job_id, current.revision),
            ).rowcount
            return JobClaim(job_id, worker_id, until, current.revision + 1) if changed == 1 else None

    def heartbeat(self, job_id: str, worker_id: str, *, lease_seconds: int = 300) -> bool:
        if not isinstance(worker_id, str) or not worker_id.strip():
            raise ValueError("worker_id must be non-empty")
        if isinstance(lease_seconds, bool) or lease_seconds < 1:
            raise ValueError("lease_seconds must be positive")
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._row(connection, job_id)
            current = self._record(row)
            if current is None or current.is_terminal:
                return False
            now = self._clock()
            if row[16] != worker_id or not self._lease_active(row[17], now):
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
               error: str = "") -> JobRecord | None:
        if not isinstance(status, JobStatus):
            raise TypeError("status must be a JobStatus")
        if status not in TERMINAL_JOB_STATUSES:
            raise ValueError("finish status must be terminal")
        if not isinstance(worker_id, str) or not worker_id.strip():
            raise ValueError("worker_id must be non-empty")
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._row(connection, job_id)
            current = self._record(row)
            if current is None or current.is_terminal:
                return current
            now = self._clock()
            if row[16] != worker_id or not self._lease_active(row[17], now):
                return None
            if status is JobStatus.SUCCEEDED and error:
                raise ValueError("successful jobs cannot carry an error")
            changed = connection.execute(
                "UPDATE durable_job SET status=?,revision=?,updated_at=?,result_json=?,error=?,"
                "worker_id=NULL,lease_until=NULL WHERE job_id=? AND revision=? AND worker_id=?",
                (status.value, current.revision + 1, now,
                 None if result is None else _json(result), error,
                 job_id, current.revision, worker_id),
            ).rowcount
            return self._record(self._row(connection, job_id)) if changed == 1 else None

    def cancel(self, job_id: str, *, reason: str = "cancelled") -> tuple[JobRecord, ...]:
        if not reason.strip():
            raise ValueError("cancellation reason is required")
        queue = [job_id]
        ids: list[str] = []
        with self._connect() as connection:
            while queue:
                current = queue.pop(0)
                if current in ids:
                    continue
                if self._row(connection, current) is None:
                    raise KeyError(f"unknown job {current!r}")
                ids.append(current)
                queue.extend(item[0] for item in connection.execute(
                    "SELECT job_id FROM durable_job WHERE parent_job_id=? ORDER BY rowid", (current,)
                ).fetchall())
        return tuple(self.transition(item, JobStatus.CANCELLED, error=reason) for item in ids)

    def collect(self, job_id: str) -> JobRecord:
        record = self.poll(job_id)
        if not record.is_terminal:
            raise ValueError("job is not terminal")
        return record

    def reconcile(self, *, now: str | None = None) -> int:
        """Mark expired worker leases interrupted and return their count."""
        current_time = self._clock() if now is None else now
        with self._lock, self._connect() as connection:
            changed = connection.execute(
                "UPDATE durable_job SET status=?, revision=revision+1, "
                "updated_at=?, worker_id=NULL, lease_until=NULL "
                "WHERE status IN (?,?) AND lease_until IS NOT NULL AND lease_until<=?",
                (JobStatus.INTERRUPTED.value, current_time,
                 JobStatus.CLAIMED.value, JobStatus.RUNNING.value, current_time),
            ).rowcount
        return changed

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


__all__ = ["JobRecoveryReport", "SQLiteDurableJobRegistry", "initialize_schema"]
