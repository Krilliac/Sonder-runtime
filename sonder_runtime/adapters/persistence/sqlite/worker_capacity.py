"""Transactional admission attached to the durable job database, not a task queue."""
from datetime import datetime, timedelta, timezone
import re
import secrets

from sonder_runtime.application.compute_fabric.capacity import (
    CapacityReservation, WorkerBudget, bounded_positive,
)
from sonder_runtime.domain.common.errors import CapacityExceeded, Conflict

CAPACITY_DDL = """
CREATE TABLE IF NOT EXISTS worker_capacity_budget (
    host_id TEXT PRIMARY KEY, memory_bytes INTEGER NOT NULL, max_jobs INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS worker_capacity_reservation (
    job_id TEXT PRIMARY KEY, host_id TEXT NOT NULL, request_sha256 TEXT NOT NULL,
    token TEXT NOT NULL, memory_bytes INTEGER NOT NULL, expires_at TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('reserved','dispatched','released')),
    FOREIGN KEY(host_id) REFERENCES worker_capacity_budget(host_id)
);
CREATE INDEX IF NOT EXISTS worker_capacity_host ON worker_capacity_reservation(host_id,state);
"""


class SQLiteWorkerCapacity:
    """Mixin using the job registry transaction/clock ports.

    Only undispatched leases age out. A dispatched row is durable uncertainty
    until the process owner supplies cleanup proof through release_capacity.
    """

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
        with self._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            now = datetime.fromisoformat(self._clock()).astimezone(timezone.utc).isoformat()
            expires = (datetime.fromisoformat(now) + timedelta(seconds=lease_seconds)).isoformat()
            legacy = connection.execute(
                "SELECT 1 FROM durable_job j WHERE j.kind LIKE 'compute-%' AND j.status NOT IN ('succeeded','failed','cancelled') AND NOT EXISTS (SELECT 1 FROM worker_capacity_reservation r WHERE r.job_id=j.job_id) LIMIT 1"
            ).fetchone()
            if legacy:
                raise CapacityExceeded('existing catalog job has unknown capacity ownership')
            existing = connection.execute(
                'SELECT host_id,request_sha256,token,memory_bytes,expires_at,state FROM worker_capacity_reservation WHERE job_id=?',
                (job_id,),
            ).fetchone()
            if existing:
                host, digest, token, memory, expiry, state = existing
                if (host, digest) != (budget.host_id, request_sha256) or (memory_bytes is not None and memory != demand):
                    raise Conflict('capacity job identity is already bound to another request')
                if state == 'reserved' and expiry > now:
                    return CapacityReservation(job_id, token, memory, expiry)
                if state != 'reserved':
                    raise Conflict('capacity job has already been dispatched or released')
            foreign_host = connection.execute(
                "SELECT 1 FROM worker_capacity_reservation WHERE host_id<>? AND (state='dispatched' OR (state='reserved' AND expires_at>?)) LIMIT 1",
                (budget.host_id, now),
            ).fetchone()
            if foreign_host:
                raise Conflict('physical host identity cannot change while capacity is occupied')
            previous = connection.execute(
                'SELECT memory_bytes,max_jobs FROM worker_capacity_budget WHERE host_id=?', (budget.host_id,),
            ).fetchone()
            occupied = connection.execute(
                "SELECT COALESCE(SUM(memory_bytes),0),COUNT(*) FROM worker_capacity_reservation WHERE host_id=? AND (state='dispatched' OR (state='reserved' AND expires_at>?))",
                (budget.host_id, now),
            ).fetchone()
            if occupied[0] + demand > budget.memory_bytes or occupied[1] >= budget.max_jobs:
                raise CapacityExceeded('worker catalog capacity is occupied')
            if previous and previous != (budget.memory_bytes, budget.max_jobs) and occupied[1]:
                raise Conflict('worker budget cannot change while reservations are occupied')
            connection.execute(
                'INSERT INTO worker_capacity_budget VALUES (?,?,?) ON CONFLICT(host_id) DO UPDATE SET memory_bytes=excluded.memory_bytes,max_jobs=excluded.max_jobs',
                (budget.host_id, budget.memory_bytes, budget.max_jobs),
            )
            token = secrets.token_hex(32)
            connection.execute(
                "INSERT INTO worker_capacity_reservation VALUES (?,?,?,?,?,?,'reserved') ON CONFLICT(job_id) DO UPDATE SET token=excluded.token,expires_at=excluded.expires_at,memory_bytes=excluded.memory_bytes",
                (job_id, budget.host_id, request_sha256, token, demand, expires),
            )
            return CapacityReservation(job_id, token, demand, expires)

    def dispatch_capacity(self, job_id, token):
        with self._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            now = datetime.fromisoformat(self._clock()).astimezone(timezone.utc).isoformat()
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
                "UPDATE worker_capacity_reservation SET state='released' WHERE job_id=? AND state='dispatched'", (job_id,),
            )
