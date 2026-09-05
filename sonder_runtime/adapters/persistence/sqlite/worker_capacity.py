"""Transactional admission attached to the durable job database, not a task queue."""
from datetime import datetime, timedelta, timezone
import re
import secrets

from sonder_runtime.application.compute_fabric.capacity import (
    CapacityReconciliation, CapacityReservation, CapacityReservationView,
    WorkerBudget, bounded_positive,
)
from sonder_runtime.domain.worker_capacity import validate_physical_host_fingerprint
from sonder_runtime.domain.common.errors import CapacityExceeded, Conflict

from .physical_identity import physical_host_identity

CAPACITY_DDL = """
CREATE TABLE IF NOT EXISTS worker_capacity_budget (
    host_id TEXT PRIMARY KEY,
    memory_bytes INTEGER NOT NULL,
    max_jobs INTEGER NOT NULL,
    physical_host_fingerprint TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS worker_capacity_reservation (
    job_id TEXT PRIMARY KEY, host_id TEXT NOT NULL, request_sha256 TEXT NOT NULL,
    token TEXT NOT NULL, memory_bytes INTEGER NOT NULL, expires_at TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('reserved','dispatched','released')),
    physical_host_fingerprint TEXT NOT NULL DEFAULT '',
    release_reason TEXT NOT NULL DEFAULT '',
    FOREIGN KEY(host_id) REFERENCES worker_capacity_budget(host_id)
);
CREATE INDEX IF NOT EXISTS worker_capacity_host ON worker_capacity_reservation(host_id,state);
"""

_MAX_RECONCILIATION_ROWS = 1024
_MAX_LIST_ROWS = 1024


def initialize_capacity_schema(connection) -> None:
    """Create the admission tables and migrate the two additive fence columns."""
    connection.executescript(CAPACITY_DDL)
    columns = {
        table: {
            row[1]
            for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        for table in ("worker_capacity_budget", "worker_capacity_reservation")
    }
    if "physical_host_fingerprint" not in columns["worker_capacity_budget"]:
        connection.execute(
            "ALTER TABLE worker_capacity_budget ADD COLUMN physical_host_fingerprint TEXT NOT NULL DEFAULT ''"
        )
    if "physical_host_fingerprint" not in columns["worker_capacity_reservation"]:
        connection.execute(
            "ALTER TABLE worker_capacity_reservation ADD COLUMN physical_host_fingerprint TEXT NOT NULL DEFAULT ''"
        )
    if "release_reason" not in columns["worker_capacity_reservation"]:
        connection.execute(
            "ALTER TABLE worker_capacity_reservation ADD COLUMN release_reason TEXT NOT NULL DEFAULT ''"
        )


class SQLiteWorkerCapacity:
    """Mixin using the job registry transaction/clock ports.

    Only undispatched leases age out. A dispatched row is durable uncertainty
    until the process owner supplies cleanup proof through release_capacity.
    """

    @staticmethod
    def _timestamp(value, label):
        if isinstance(value, datetime):
            current = value
        elif isinstance(value, str):
            try:
                current = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError(f"{label} must be an ISO timestamp") from exc
        else:
            raise ValueError(f"{label} must be a timezone-aware timestamp")
        if current.tzinfo is None or current.utcoffset() is None:
            raise ValueError(f"{label} must be timezone-aware")
        return current.astimezone(timezone.utc).isoformat()

    @staticmethod
    def _fingerprint(budget):
        if budget.physical_host_fingerprint is not None:
            validate_physical_host_fingerprint(budget.physical_host_fingerprint)
            return budget.physical_host_fingerprint
        return physical_host_identity(budget.host_id).fingerprint

    @staticmethod
    def _view(row):
        (
            job_id,
            host_id,
            request_sha256,
            _token,
            memory_bytes,
            expires_at,
            state,
            fingerprint,
            release_reason,
        ) = row
        return CapacityReservationView(
            job_id=job_id,
            host_id=host_id,
            request_sha256=request_sha256,
            memory_bytes=memory_bytes,
            expires_at=expires_at,
            state=state,
            physical_host_fingerprint=fingerprint or None,
            release_reason=release_reason or "",
        )

    def _reconcile_locked(self, connection, now, *, limit):
        rows = connection.execute(
            "SELECT job_id,host_id,request_sha256,token,memory_bytes,expires_at,state,"
            "physical_host_fingerprint,release_reason "
            "FROM worker_capacity_reservation "
            "WHERE state='reserved' AND expires_at<=? "
            "ORDER BY expires_at,job_id LIMIT ?",
            (now, limit),
        ).fetchall()
        if not rows:
            return ()
        connection.executemany(
            "UPDATE worker_capacity_reservation SET state='released',release_reason='expired' "
            "WHERE job_id=? AND state='reserved' AND expires_at<=?",
            ((row[0], now) for row in rows),
        )
        return tuple(
            self._view((*row[:6], "released", row[7], "expired")) for row in rows
        )

    def reconcile_capacity(self, *, now=None, limit=_MAX_RECONCILIATION_ROWS):
        """Expire only reserved leases, retaining dispatched uncertainty.

        Reconciliation is deliberately bounded.  A caller can run another
        pass when ``inspected`` reaches the limit; occupancy checks remain
        correct even between passes because they compare expiry timestamps.
        """
        if type(limit) is not int or not 1 <= limit <= _MAX_RECONCILIATION_ROWS:
            raise ValueError(
                f"capacity reconciliation limit must be within 1..{_MAX_RECONCILIATION_ROWS}"
            )
        current = self._timestamp(self._clock() if now is None else now, "now")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            expired = self._reconcile_locked(connection, current, limit=limit)
            return CapacityReconciliation(current, expired, len(expired))

    def list_capacity(self, *, host_id=None, include_released=False, limit=256):
        """Return bounded redacted reservation state for operator reconciliation."""
        if type(limit) is not int or not 1 <= limit <= _MAX_LIST_ROWS:
            raise ValueError(f"capacity list limit must be within 1..{_MAX_LIST_ROWS}")
        clauses = []
        parameters = []
        if host_id is not None:
            if not isinstance(host_id, str) or not host_id:
                raise ValueError("capacity host_id must be non-empty")
            clauses.append("host_id=?")
            parameters.append(host_id)
        if not include_released:
            clauses.append("state<>'released'")
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        parameters.append(limit)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT job_id,host_id,request_sha256,token,memory_bytes,expires_at,state,"
                "physical_host_fingerprint,release_reason FROM worker_capacity_reservation"
                + where + " ORDER BY expires_at,job_id LIMIT ?",
                tuple(parameters),
            ).fetchall()
        return tuple(self._view(row) for row in rows)

    def reserve_capacity(self, budget, job_id, request_sha256, memory_bytes, *, lease_seconds=30):
        if not isinstance(budget, WorkerBudget):
            raise TypeError('budget must be WorkerBudget')
        if not isinstance(job_id, str) or not job_id or len(job_id) > 256:
            raise ValueError('job_id must be bounded')
        if not isinstance(request_sha256, str) or not re.fullmatch('[0-9a-f]{64}', request_sha256):
            raise ValueError('request_sha256 must be SHA-256')
        bounded_positive(lease_seconds, 'reservation lease_seconds', 300)
        demand = budget.memory_bytes if memory_bytes is None else memory_bytes
        if memory_bytes is not None:
            bounded_positive(memory_bytes, 'memory reservation')
        if budget.memory_bytes == 0 or demand > budget.memory_bytes:
            raise CapacityExceeded('worker RAM budget is unconfigured or insufficient')
        fingerprint = self._fingerprint(budget)
        with self._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            now = self._timestamp(self._clock(), "clock")
            self._reconcile_locked(connection, now, limit=_MAX_RECONCILIATION_ROWS)
            expires = (datetime.fromisoformat(now) + timedelta(seconds=lease_seconds)).isoformat()
            legacy = connection.execute(
                "SELECT 1 FROM durable_job j WHERE j.kind LIKE 'compute-%' AND j.status NOT IN ('succeeded','failed','cancelled') AND NOT EXISTS (SELECT 1 FROM worker_capacity_reservation r WHERE r.job_id=j.job_id) LIMIT 1"
            ).fetchone()
            if legacy:
                raise CapacityExceeded('existing catalog job has unknown capacity ownership')
            existing = connection.execute(
                'SELECT host_id,request_sha256,token,memory_bytes,expires_at,state,physical_host_fingerprint,release_reason FROM worker_capacity_reservation WHERE job_id=?',
                (job_id,),
            ).fetchone()
            if existing:
                host, digest, token, memory, expiry, state, existing_fingerprint, release_reason = existing
                if (host, digest) != (budget.host_id, request_sha256) or (memory_bytes is not None and memory != demand):
                    raise Conflict('capacity job identity is already bound to another request')
                if existing_fingerprint and existing_fingerprint != fingerprint:
                    raise Conflict('physical host identity changed for capacity job')
                reusable = state == 'reserved' and expiry <= now and release_reason in ("", "expired")
                if state == 'reserved' and expiry > now:
                    return CapacityReservation(job_id, token, memory, expiry, fingerprint, state)
                if state == 'released' and release_reason == 'expired':
                    reusable = True
                if not reusable:
                    raise Conflict('capacity job has already been dispatched or released')
            foreign_host = connection.execute(
                "SELECT 1 FROM worker_capacity_reservation WHERE host_id<>? AND (state='dispatched' OR (state='reserved' AND expires_at>?)) LIMIT 1",
                (budget.host_id, now),
            ).fetchone()
            if foreign_host:
                raise Conflict('physical host identity cannot change while capacity is occupied')
            foreign_fingerprint = connection.execute(
                "SELECT 1 FROM worker_capacity_reservation WHERE physical_host_fingerprint NOT IN (?, '') "
                "AND (state='dispatched' OR (state='reserved' AND expires_at>?)) LIMIT 1",
                (fingerprint, now),
            ).fetchone()
            if foreign_fingerprint:
                raise Conflict('physical host identity cannot change while capacity is occupied')
            previous = connection.execute(
                'SELECT memory_bytes,max_jobs,physical_host_fingerprint FROM worker_capacity_budget WHERE host_id=?', (budget.host_id,),
            ).fetchone()
            occupied = connection.execute(
                "SELECT COALESCE(SUM(memory_bytes),0),COUNT(*) FROM worker_capacity_reservation WHERE host_id=? AND (state='dispatched' OR (state='reserved' AND expires_at>?))",
                (budget.host_id, now),
            ).fetchone()
            if occupied[0] + demand > budget.memory_bytes or occupied[1] >= budget.max_jobs:
                raise CapacityExceeded('worker catalog capacity is occupied')
            if previous and previous[2] and previous[2] != fingerprint:
                raise Conflict('physical host identity changed for capacity authority')
            if previous and not previous[2] and occupied[1]:
                raise Conflict('physical host identity is unverified for occupied capacity')
            if previous and previous[:2] != (budget.memory_bytes, budget.max_jobs) and occupied[1]:
                raise Conflict('worker budget cannot change while reservations are occupied')
            connection.execute(
                'INSERT INTO worker_capacity_budget (host_id,memory_bytes,max_jobs,physical_host_fingerprint) VALUES (?,?,?,?) '
                'ON CONFLICT(host_id) DO UPDATE SET memory_bytes=excluded.memory_bytes,max_jobs=excluded.max_jobs,physical_host_fingerprint=excluded.physical_host_fingerprint',
                (budget.host_id, budget.memory_bytes, budget.max_jobs, fingerprint),
            )
            token = secrets.token_hex(32)
            if existing:
                connection.execute(
                    "UPDATE worker_capacity_reservation SET host_id=?,request_sha256=?,token=?,memory_bytes=?,expires_at=?,state='reserved',physical_host_fingerprint=?,release_reason='' WHERE job_id=?",
                    (budget.host_id, request_sha256, token, demand, expires, fingerprint, job_id),
                )
            else:
                connection.execute(
                    "INSERT INTO worker_capacity_reservation (job_id,host_id,request_sha256,token,memory_bytes,expires_at,state,physical_host_fingerprint,release_reason) VALUES (?,?,?,?,?,?,'reserved',?,'')",
                    (job_id, budget.host_id, request_sha256, token, demand, expires, fingerprint),
                )
            return CapacityReservation(job_id, token, demand, expires, fingerprint, "reserved")

    def dispatch_capacity(self, job_id, token):
        with self._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            now = self._timestamp(self._clock(), "clock")
            result = connection.execute(
                "UPDATE worker_capacity_reservation SET state='dispatched' WHERE job_id=? AND token=? AND state='reserved' AND expires_at>?",
                (job_id, token, now),
            )
            if result.rowcount != 1:
                raise Conflict('worker capacity reservation is expired, consumed, or invalid')

    def release_capacity(self, job_id):
        # Trusted process-owner API: callers must already possess cleanup proof.
        # Neither job status transitions nor lease reconciliation call this.
        with self._connect() as connection:
            connection.execute(
                "UPDATE worker_capacity_reservation SET state='released',release_reason='cleanup-proven' WHERE job_id=? AND state='dispatched'", (job_id,),
            )
