"""Machine-wide SQLite state behind GPU admission.

Every Sonder process on a box shares one GPU, so they share one queue: this file
holds the lease table (waiting and held tickets), the one-window-per-model pins
and the measured host footprints. It lives beside the user's local app data, not
in a per-instance state home, so a worktree instance with its own home still
queues behind production. For the same reason it opens its own connections
instead of the per-home owned-SQLite namespace.

Ownership and thread affinity: a ``GpuAdmissionStore`` holds no connection; each
operation opens one, runs a single ``BEGIN IMMEDIATE`` transaction and closes it,
so it is safe to call from any thread and from several processes at once.

Failure stance (as ``model_request_admission``): unreadable, corrupt or
wrong-schema state raises ``StoreUnavailable``; it is never silently reset,
because a reset would hand out every slot again while another process still
holds one. A holder is reclaimed only when its process is provably gone (pid
liveness with creation-time identity) or its own lease deadline has passed.
"""
from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from sonder_runtime.adapters.process_liveness import (
    PROCESS_DEAD, probe_process, process_identity,
)
from sonder_runtime.domain.gpu_admission import QueueEntry, queue_order
from sonder_runtime.platform.private_files import prepare_private_sqlite

SCHEMA_VERSION = "1"
HELD = "held"
WAITING = "waiting"
FULL = "full"

_SCHEMA = """
CREATE TABLE gpu_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE gpu_leases(
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id TEXT NOT NULL UNIQUE,
    pool TEXT NOT NULL,
    state TEXT NOT NULL,
    priority INTEGER NOT NULL,
    pid INTEGER NOT NULL,
    pid_identity TEXT,
    origin TEXT NOT NULL,
    model_key TEXT NOT NULL,
    enqueued_at REAL NOT NULL,
    acquired_at REAL,
    expires_at REAL NOT NULL
);
CREATE TABLE gpu_pins(
    origin TEXT NOT NULL,
    model_key TEXT NOT NULL,
    num_ctx INTEGER NOT NULL,
    pinned_at REAL NOT NULL,
    last_used REAL NOT NULL,
    repins INTEGER NOT NULL DEFAULT 0,
    repin_window_start REAL NOT NULL,
    PRIMARY KEY(origin, model_key)
);
CREATE TABLE gpu_footprints(
    digest TEXT NOT NULL,
    num_ctx INTEGER NOT NULL,
    working_set INTEGER NOT NULL,
    private_bytes INTEGER NOT NULL,
    measured_at REAL NOT NULL,
    PRIMARY KEY(digest, num_ctx)
);
"""


class StoreUnavailable(RuntimeError):
    """The shared admission state cannot be read or does not match this build."""


@dataclass(frozen=True, slots=True)
class PinRecord:
    num_ctx: int
    pinned_at: float
    last_used: float
    repins: int
    repin_window_start: float


@dataclass(frozen=True, slots=True)
class FootprintRecord:
    digest: str
    num_ctx: int
    working_set_bytes: int
    private_bytes: int
    measured_at: float


@dataclass(frozen=True, slots=True)
class LeaseRow:
    ticket_id: str
    pool: str
    state: str
    pid: int
    origin: str
    model_key: str


class GpuAdmissionStore:
    def __init__(
        self, path: str | os.PathLike[str], *,
        clock: Callable[[], float] = time.time,
        pid: int | None = None,
        liveness: Callable[..., tuple[str, str | None]] = probe_process,
        busy_timeout_seconds: float = 5.0,
    ) -> None:
        self.path = Path(path)
        self._clock = clock
        self._pid = os.getpid() if pid is None else int(pid)
        self._liveness = liveness
        self._busy = float(busy_timeout_seconds)
        self._identity: str | None = None
        self._checked = False
        self._private = False

    # -- connection plumbing ------------------------------------------------
    def _identity_of_self(self) -> str | None:
        if self._identity is None:
            self._identity = process_identity(self._pid) or ""
        return self._identity or None

    def _connect(self) -> sqlite3.Connection:
        if self.path.is_symlink():
            raise StoreUnavailable("admission store must not be a symlink")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        return sqlite3.connect(str(self.path), timeout=self._busy)

    def _begin(self, connection: sqlite3.Connection) -> None:
        connection.execute("BEGIN IMMEDIATE")
        if self._checked:
            return
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        if not tables:
            for statement in _SCHEMA.split(";"):
                if statement.strip():
                    connection.execute(statement)
            connection.execute("INSERT INTO gpu_meta(key,value) VALUES('schema_version',?)",
                               (SCHEMA_VERSION,))
        else:
            row = None
            if "gpu_meta" in tables:
                row = connection.execute(
                    "SELECT value FROM gpu_meta WHERE key='schema_version'").fetchone()
            if row is None or row[0] != SCHEMA_VERSION:
                raise StoreUnavailable("admission store schema mismatch (expected version %s)"
                                       % SCHEMA_VERSION)
        self._checked = True

    def _run(self, work: Callable[[sqlite3.Connection, float], object]):
        connection = None
        try:
            connection = self._connect()
            self._begin(connection)
            result = work(connection, float(self._clock()))
            connection.commit()
            self._restrict_once()
            return result
        except StoreUnavailable:
            self._rollback(connection)
            raise
        except (sqlite3.Error, OSError, ValueError, OverflowError) as error:
            self._rollback(connection)
            raise StoreUnavailable("admission store unavailable: %s" % type(error).__name__) from error
        finally:
            if connection is not None:
                connection.close()

    def _restrict_once(self) -> None:
        if not self._private:
            self._private = True
            try:
                prepare_private_sqlite(str(self.path), create=False)
            except OSError:
                pass

    @staticmethod
    def _rollback(connection) -> None:
        if connection is not None:
            try:
                connection.rollback()
            except sqlite3.Error:
                pass

    def verify(self) -> None:
        """Open the store and check (or create) its schema; raises on a bad one."""
        self._run(lambda connection, now: None)

    # -- leases ---------------------------------------------------------------
    def _reap(self, connection: sqlite3.Connection, now: float) -> None:
        rows = connection.execute(
            "SELECT ticket_id,pid,pid_identity,expires_at FROM gpu_leases").fetchall()
        verdicts: dict[tuple[int, str | None], bool] = {}
        for ticket_id, pid, identity, expires_at in rows:
            if expires_at <= now:
                dead = True
            elif pid == self._pid:
                dead = False
            else:
                key = (pid, identity)
                if key not in verdicts:
                    state, _observed = self._liveness(pid, identity or None)
                    verdicts[key] = state == PROCESS_DEAD
                dead = verdicts[key]
            if dead:
                connection.execute("DELETE FROM gpu_leases WHERE ticket_id=?", (ticket_id,))

    def acquire(
        self, ticket_id: str, pool: str, *, slots: int, priority: int, origin: str,
        model_key: str, ttl_seconds: float, max_depth: int,
    ) -> str:
        """Join (or poll) the queue for ``pool``; ``HELD``, ``WAITING`` or ``FULL``.

        Idempotent per ``ticket_id``: calling again is the poll. A ticket is held
        when it is among the first ``slots - held`` waiters in service order
        (interactive before background, FIFO within a class).
        """
        def work(connection, now):
            self._reap(connection, now)
            row = connection.execute(
                "SELECT state FROM gpu_leases WHERE ticket_id=?", (ticket_id,)).fetchone()
            if row is None:
                waiting = connection.execute(
                    "SELECT COUNT(*) FROM gpu_leases WHERE pool=? AND state=?",
                    (pool, WAITING)).fetchone()[0]
                if waiting >= max_depth:
                    return FULL
                connection.execute(
                    "INSERT INTO gpu_leases(ticket_id,pool,state,priority,pid,pid_identity,origin,"
                    "model_key,enqueued_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (ticket_id, pool, WAITING, int(priority), self._pid, self._identity_of_self(),
                     origin, model_key, now, now + ttl_seconds))
                row = (WAITING,)
            if row[0] == HELD:
                connection.execute("UPDATE gpu_leases SET expires_at=? WHERE ticket_id=?",
                                   (now + ttl_seconds, ticket_id))
                return HELD
            held = connection.execute(
                "SELECT COUNT(*) FROM gpu_leases WHERE pool=? AND state=?", (pool, HELD)).fetchone()[0]
            free = max(0, int(slots) - held)
            waiters = connection.execute(
                "SELECT ticket_id,priority,seq FROM gpu_leases WHERE pool=? AND state=?",
                (pool, WAITING)).fetchall()
            order = queue_order(QueueEntry(t, p, s) for t, p, s in waiters)
            if any(entry.ticket_id == ticket_id for entry in order[:free]):
                connection.execute(
                    "UPDATE gpu_leases SET state=?,acquired_at=?,expires_at=? WHERE ticket_id=?",
                    (HELD, now, now + ttl_seconds, ticket_id))
                return HELD
            connection.execute("UPDATE gpu_leases SET expires_at=? WHERE ticket_id=?",
                               (now + ttl_seconds, ticket_id))
            return WAITING

        return self._run(work)

    def release(self, ticket_id: str) -> None:
        self._run(lambda connection, now: connection.execute(
            "DELETE FROM gpu_leases WHERE ticket_id=?", (ticket_id,)))

    def holders(self, origin: str, model_key: str, *, exclude: str = "") -> int:
        """Held tickets (other than ``exclude``) using this model on this origin."""
        def work(connection, now):
            self._reap(connection, now)
            return connection.execute(
                "SELECT COUNT(*) FROM gpu_leases WHERE state=? AND origin=? AND model_key=? "
                "AND ticket_id<>?", (HELD, origin, model_key, exclude)).fetchone()[0]

        return int(self._run(work))

    def leases(self) -> tuple[LeaseRow, ...]:
        def work(connection, now):
            self._reap(connection, now)
            return connection.execute(
                "SELECT ticket_id,pool,state,pid,origin,model_key FROM gpu_leases ORDER BY seq"
            ).fetchall()

        return tuple(LeaseRow(*row) for row in self._run(work))

    # -- pins -----------------------------------------------------------------
    def get_pin(self, origin: str, model_key: str) -> PinRecord | None:
        row = self._run(lambda connection, now: connection.execute(
            "SELECT num_ctx,pinned_at,last_used,repins,repin_window_start FROM gpu_pins "
            "WHERE origin=? AND model_key=?", (origin, model_key)).fetchone())
        return None if row is None else PinRecord(*row)

    def set_pin(self, origin: str, model_key: str, num_ctx: int, *, repin: bool = False,
                window_seconds: int = 600) -> None:
        def work(connection, now):
            row = connection.execute(
                "SELECT repins,repin_window_start FROM gpu_pins WHERE origin=? AND model_key=?",
                (origin, model_key)).fetchone()
            repins, window_start = (0, now) if row is None else row
            if now - window_start > window_seconds:
                repins, window_start = 0, now
            if repin:
                repins += 1
            connection.execute(
                "INSERT INTO gpu_pins(origin,model_key,num_ctx,pinned_at,last_used,repins,"
                "repin_window_start) VALUES(?,?,?,?,?,?,?) ON CONFLICT(origin,model_key) DO UPDATE "
                "SET num_ctx=excluded.num_ctx,pinned_at=excluded.pinned_at,last_used=excluded.last_used,"
                "repins=excluded.repins,repin_window_start=excluded.repin_window_start",
                (origin, model_key, int(num_ctx), now, now, repins, window_start))

        self._run(work)

    def touch_pin(self, origin: str, model_key: str) -> None:
        self._run(lambda connection, now: connection.execute(
            "UPDATE gpu_pins SET last_used=? WHERE origin=? AND model_key=?", (now, origin, model_key)))

    def repins_in_window(self, origin: str, model_key: str, window_seconds: int) -> int:
        pin = self.get_pin(origin, model_key)
        if pin is None or float(self._clock()) - pin.repin_window_start > window_seconds:
            return 0
        return pin.repins

    def clear_pin(self, origin: str, model_key: str) -> None:
        self._run(lambda connection, now: connection.execute(
            "DELETE FROM gpu_pins WHERE origin=? AND model_key=?", (origin, model_key)))

    def pins(self) -> dict[tuple[str, str], int]:
        rows = self._run(lambda connection, now: connection.execute(
            "SELECT origin,model_key,num_ctx FROM gpu_pins").fetchall())
        return {(origin, model): ctx for origin, model, ctx in rows}

    # -- footprints -----------------------------------------------------------
    def record_footprint(self, digest: str, num_ctx: int, working_set_bytes: int,
                         private_bytes: int) -> None:
        """Store the larger of the previous and the new measurement."""
        def work(connection, now):
            connection.execute(
                "INSERT INTO gpu_footprints(digest,num_ctx,working_set,private_bytes,measured_at) "
                "VALUES(?,?,?,?,?) ON CONFLICT(digest,num_ctx) DO UPDATE SET "
                "working_set=MAX(working_set,excluded.working_set),"
                "private_bytes=MAX(private_bytes,excluded.private_bytes),"
                "measured_at=excluded.measured_at",
                (digest, int(num_ctx), int(working_set_bytes), int(private_bytes), now))

        self._run(work)

    def footprints_for(self, digest: str) -> tuple[FootprintRecord, ...]:
        rows = self._run(lambda connection, now: connection.execute(
            "SELECT digest,num_ctx,working_set,private_bytes,measured_at FROM gpu_footprints "
            "WHERE digest=?", (digest,)).fetchall())
        return tuple(FootprintRecord(*row) for row in rows)


__all__ = [
    "FULL", "FootprintRecord", "GpuAdmissionStore", "HELD", "LeaseRow", "PinRecord",
    "SCHEMA_VERSION", "StoreUnavailable", "WAITING",
]
