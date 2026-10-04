"""Durable registry of leases on named external resources.

The policy (what a lease is, when one may be reclaimed) lives in
``domain.resource_leases``. This adapter stores leases in one SQLite file so
separate worker *processes* see each other's holds, and supplies the evidence
the policy asks for:

* owner liveness: ``probe_process`` on the recorded owner pid (and identity);
  a probe that cannot decide counts as alive, so an unreadable owner is never
  declared dead;
* cleanup evidence, per resource kind: for a ``port`` the port can be bound
  again on ``127.0.0.1`` (and on the wildcard address); for everything else
  the lease's metadata names what would still be holding it -- ``pid`` (a
  process that must be proven dead, checked against ``pid_identity`` when
  recorded), ``port`` (a port that must be proven free) and ``path`` (a file
  or directory that must be gone). A ``desktop_session`` lease that records
  no indicator ``pid`` yet is proven released by its owner's death alone:
  the indicator is only ever launched by that owner and exits with it. Any
  other lease whose metadata names none of these has no cleanup evidence and
  is never reclaimed; its owner releases it. Callers may register a
  kind-specific check instead.

The database is opened (and its table created) on first use, not on
construction, so wiring a registry into a consumer costs nothing until a
lease is actually taken.

Every mutation runs inside ``BEGIN IMMEDIATE`` so two processes racing for the
same resource serialize on the database lock: exactly one wins.
"""
from __future__ import annotations

import json
from contextlib import suppress
import logging
import os
import secrets
import socket
import sqlite3
import time
from pathlib import Path
from typing import Callable, Iterable, Mapping

from ..domain.common.errors import DependencyUnavailable
from ..domain.resource_leases import (
    KIND_DESKTOP_SESSION, KIND_PORT, RESOURCE_LEASE_BUSY, RESOURCE_LEASE_EXHAUSTED, RESOURCE_LEASE_NOT_FOUND,
    RESOURCE_LEASE_NOT_OWNER, ResourceLease, lease_error, reclaim_decision,
    validate_kind, validate_metadata, validate_port_range, validate_text, validate_ttl,
)
from .persistence.sqlite_factory import connect as sqlite_connect
from .process_liveness import PROCESS_DEAD, probe_process

logger = logging.getLogger(__name__)

DEFAULT_PORT_RANGE = (47000, 47999)
LOOPBACK_HOST = "127.0.0.1"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS resource_leases (
    kind TEXT NOT NULL,
    key TEXT NOT NULL,
    lease_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    record TEXT NOT NULL,
    PRIMARY KEY (kind, key)
)
"""

# (lease) -> True only when the resource is proven released.
CleanupEvidence = Callable[[ResourceLease], bool]


def _bindable(host: str, port: int) -> bool:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        probe.bind((host, int(port)))
        return True
    except (OSError, OverflowError):
        return False
    finally:
        probe.close()


def port_is_free(port: int, host: str = LOOPBACK_HOST) -> bool:
    """True only when ``host:port`` and the wildcard address can both be bound now.

    A bind test on ``127.0.0.1`` alone is not enough on Windows, where it
    succeeds beside a listener on ``0.0.0.0``; binding the wildcard address
    too (exclusively, on Windows) closes that gap. No connect probe: a refused
    loopback connect takes about two seconds on Windows. Any error counts as
    "not free".
    """
    return _bindable(host, port) and _bindable("0.0.0.0", port)


def _owner_dead(lease: ResourceLease, probe) -> bool:
    if lease.owner_pid is None:
        return False
    state, _identity = probe(lease.owner_pid, lease.owner_identity)
    return state == PROCESS_DEAD


class SqliteResourceLeaseRegistry:
    """Acquire, heartbeat, release and (narrowly) reclaim resource leases."""

    def __init__(self, path: str | Path, *, clock: Callable[[], float] = time.time,
                 probe=probe_process, port_free: Callable[[int], bool] = port_is_free,
                 path_exists: Callable[[str], bool] = os.path.exists,
                 port_range: tuple[int, int] = DEFAULT_PORT_RANGE,
                 evidence: Mapping[str, CleanupEvidence] | None = None):
        self._path = Path(path)
        self._clock = clock
        self._probe = probe
        self._port_free = port_free
        self._path_exists = path_exists
        self._port_range = validate_port_range(port_range)
        self._evidence: dict[str, CleanupEvidence] = dict(evidence or {})
        self._schema_ready = False

    # -- storage -----------------------------------------------------------

    def _connection(self):
        registry = self

        class _Txn:
            def __enter__(self_inner):
                conn = None
                try:
                    conn = sqlite_connect(registry._path, timeout=10.0, busy_timeout_ms=10000)
                    conn.isolation_level = None
                    if not registry._schema_ready:
                        conn.execute(_SCHEMA)
                        registry._schema_ready = True
                    conn.execute("BEGIN IMMEDIATE")
                except sqlite3.Error as exc:
                    # A failed __enter__ never reaches __exit__. Release the
                    # connection immediately, including ordinary busy errors.
                    if conn is not None:
                        with suppress(sqlite3.Error):
                            conn.close()
                    raise DependencyUnavailable("resource lease store unavailable: %s" % exc) from None
                self_inner.conn = conn
                return conn

            def __exit__(self_inner, exc_type, exc, tb):
                conn = self_inner.conn
                try:
                    conn.execute("ROLLBACK" if exc_type else "COMMIT")
                except sqlite3.Error as commit_error:
                    if exc_type is None:
                        raise DependencyUnavailable(
                            "resource lease store unavailable: %s" % commit_error) from None
                finally:
                    conn.close()
                if exc_type is not None and issubclass(exc_type, sqlite3.Error):
                    raise DependencyUnavailable("resource lease store unavailable: %s" % exc) from None
                return False

        return _Txn()

    @staticmethod
    def _row(conn, kind: str, key: str) -> ResourceLease | None:
        row = conn.execute("SELECT record FROM resource_leases WHERE kind=? AND key=?",
                           (kind, key)).fetchone()
        return ResourceLease.from_record(json.loads(row[0])) if row else None

    @staticmethod
    def _put(conn, lease: ResourceLease) -> None:
        conn.execute(
            "INSERT OR REPLACE INTO resource_leases(kind, key, lease_id, owner_id, record)"
            " VALUES (?, ?, ?, ?, ?)",
            (lease.kind, lease.key, lease.lease_id, lease.owner_id,
             json.dumps(lease.to_record(), sort_keys=True)))

    @staticmethod
    def _delete(conn, lease: ResourceLease) -> None:
        conn.execute("DELETE FROM resource_leases WHERE kind=? AND key=? AND lease_id=?",
                     (lease.kind, lease.key, lease.lease_id))

    # -- evidence ----------------------------------------------------------

    def register_evidence(self, kind: str, check: CleanupEvidence) -> None:
        """Supply the cleanup check for a kind (extends the built-in kinds)."""
        self._evidence[validate_kind(kind)] = check

    def cleanup_evidence(self, lease: ResourceLease) -> bool:
        """True only when something positively proves the resource was released."""
        check = self._evidence.get(lease.kind)
        if check is not None:
            try:
                return bool(check(lease))
            except Exception:  # an evidence check that fails proves nothing
                logger.debug("cleanup evidence check failed for %s", lease.resource, exc_info=True)
                return False
        proofs: list[bool] = []
        if lease.kind == KIND_PORT and lease.key.isascii() and lease.key.isdigit():
            proofs.append(self._port_free(int(lease.key)))
        meta = lease.metadata
        pid = meta.get("pid")
        if isinstance(pid, int) and not isinstance(pid, bool):
            state, _ = self._probe(pid, meta.get("pid_identity") or None)
            proofs.append(state == PROCESS_DEAD)
        port = meta.get("port")
        if isinstance(port, int) and not isinstance(port, bool):
            proofs.append(self._port_free(port))
        path = meta.get("path")
        if isinstance(path, str) and path:
            proofs.append(not self._path_exists(path))
        if not proofs and lease.kind == KIND_DESKTOP_SESSION:
            # No indicator was recorded: nothing but the owner can be driving.
            return _owner_dead(lease, self._probe)
        return bool(proofs) and all(proofs)

    def _try_reclaim(self, conn, held: ResourceLease) -> tuple[bool, str]:
        owner_dead = _owner_dead(held, self._probe)
        expired = held.expired(self._clock())
        decision = reclaim_decision(
            owner_dead=owner_dead, ttl_expired=expired,
            cleanup_evidence=(owner_dead or expired) and self.cleanup_evidence(held))
        if decision.reclaim:
            self._delete(conn, held)
            logger.info("reclaimed resource lease %s from %s: %s",
                        held.resource, held.owner_id, decision.reason)
        return decision.reclaim, decision.reason

    # -- operations --------------------------------------------------------

    def _new_lease(self, kind: str, key: str, owner_id: str, ttl: float,
                   owner_pid, owner_identity, metadata: dict) -> ResourceLease:
        now = self._clock()
        return ResourceLease(
            lease_id="rl-" + secrets.token_hex(12), kind=kind, key=key, owner_id=owner_id,
            acquired_at=now, expires_at=now + ttl, ttl_seconds=ttl,
            owner_pid=owner_pid, owner_identity=owner_identity, heartbeat_at=now,
            metadata=metadata)

    def acquire(self, kind: str, key: str, owner_id: str, *, ttl_seconds: float,
                owner_pid: int | None = None, owner_identity: str | None = None,
                metadata: Mapping | None = None) -> ResourceLease:
        """Hold ``kind:key`` for ``owner_id``; raises ``RESOURCE_LEASE_BUSY`` when held.

        Re-acquiring a lease the same owner already holds renews it. Its
        process evidence stays pinned: a restarted worker needs an explicit
        release or a fresh owner id and the normal cleanup/reclaim rule.
        """
        kind, key = validate_kind(kind), validate_text("key", key)
        owner_id = validate_text("owner_id", owner_id)
        ttl = validate_ttl(ttl_seconds)
        meta = validate_metadata(metadata)
        if kind == KIND_PORT:
            port = _port_key(key)
            validate_port_range((port, port))
            key = str(port)  # "047000" and "47000" are the same port
        with self._connection() as conn:
            held = self._row(conn, kind, key)
            if held is not None and held.owner_id == owner_id:
                if ((owner_pid is not None and owner_pid != held.owner_pid)
                        or (owner_identity is not None and owner_identity != held.owner_identity)):
                    raise lease_error(RESOURCE_LEASE_NOT_OWNER,
                                      "resource lease owner process changed; release before reacquiring")
                renewed = self._renewed(held, ttl, meta or None, now=self._clock())
                self._put(conn, renewed)
                return renewed
            if held is not None:
                reclaimed, reason = self._try_reclaim(conn, held)
                if not reclaimed:
                    raise lease_error(RESOURCE_LEASE_BUSY, "%s is held by %s (%s)"
                                      % (held.resource, held.owner_id, reason))
            if kind == KIND_PORT and not self._port_free(port):
                raise lease_error(RESOURCE_LEASE_BUSY,
                                  "port %s is in use outside the lease registry" % key)
            lease = self._new_lease(kind, key, owner_id, ttl, owner_pid, owner_identity, meta)
            self._put(conn, lease)
            return lease

    def acquire_port(self, owner_id: str, *, ttl_seconds: float,
                     owner_pid: int | None = None, owner_identity: str | None = None,
                     metadata: Mapping | None = None,
                     port_range: tuple[int, int] | None = None) -> ResourceLease:
        """Lease the first port in the range that is unleased (or reclaimable) and bind-free."""
        owner_id = validate_text("owner_id", owner_id)
        ttl = validate_ttl(ttl_seconds)
        meta = validate_metadata(metadata)
        low, high = validate_port_range(port_range or self._port_range)
        with self._connection() as conn:
            held_keys = {row[0] for row in conn.execute(
                "SELECT key FROM resource_leases WHERE kind=?", (KIND_PORT,))}
            for port in range(low, high + 1):
                key = str(port)
                if key in held_keys:
                    held = self._row(conn, KIND_PORT, key)
                    if held is not None and not self._try_reclaim(conn, held)[0]:
                        continue
                if not self._port_free(port):
                    continue
                lease = self._new_lease(KIND_PORT, key, owner_id, ttl, owner_pid,
                                        owner_identity, meta)
                self._put(conn, lease)
                return lease
        raise lease_error(RESOURCE_LEASE_EXHAUSTED,
                          "no free port in %d-%d" % (low, high))

    @staticmethod
    def _renewed(held: ResourceLease, ttl: float | None, metadata: dict | None,
                 now: float | None = None) -> ResourceLease:
        now = held.heartbeat_at if now is None else now
        ttl = held.ttl_seconds if ttl is None else ttl
        record = held.to_record()
        record.update(ttl_seconds=ttl, heartbeat_at=now, expires_at=now + ttl)
        if metadata is not None:
            record["metadata"] = metadata
        return ResourceLease.from_record(record)

    def heartbeat(self, kind: str, key: str, owner_id: str, *,
                  ttl_seconds: float | None = None) -> ResourceLease:
        """Extend the owner's lease by its TTL from now; only the owner may."""
        kind, key = _normal_key(kind, key)
        ttl = validate_ttl(ttl_seconds) if ttl_seconds is not None else None
        with self._connection() as conn:
            held = self._require_owned(conn, kind, key, owner_id)
            renewed = self._renewed(held, ttl, None, now=self._clock())
            self._put(conn, renewed)
            return renewed

    def release(self, kind: str, key: str, owner_id: str) -> bool:
        """Drop the owner's lease. False when nothing is held; refuses other owners."""
        kind, key = _normal_key(kind, key)
        with self._connection() as conn:
            held = self._row(conn, kind, key)
            if held is None:
                return False
            if held.owner_id != owner_id:
                raise lease_error(RESOURCE_LEASE_NOT_OWNER, "%s is held by %s, not %s"
                                  % (held.resource, held.owner_id, owner_id))
            self._delete(conn, held)
            return True

    def _require_owned(self, conn, kind: str, key: str, owner_id: str) -> ResourceLease:
        held = self._row(conn, kind, key)
        if held is None:
            raise lease_error(RESOURCE_LEASE_NOT_FOUND, "%s:%s is not leased" % (kind, key))
        if held.owner_id != owner_id:
            raise lease_error(RESOURCE_LEASE_NOT_OWNER, "%s is held by %s, not %s"
                              % (held.resource, held.owner_id, owner_id))
        return held

    def holder(self, kind: str, key: str) -> ResourceLease | None:
        kind, key = _normal_key(kind, key)
        with self._connection() as conn:
            return self._row(conn, kind, key)

    def leases(self, kind: str | None = None) -> tuple[ResourceLease, ...]:
        with self._connection() as conn:
            if kind is None:
                rows = conn.execute("SELECT record FROM resource_leases ORDER BY kind, key")
            else:
                rows = conn.execute("SELECT record FROM resource_leases WHERE kind=? ORDER BY key",
                                    (validate_kind(kind),))
            return tuple(ResourceLease.from_record(json.loads(r[0])) for r in rows)

    def sweep(self, kinds: Iterable[str] | None = None) -> tuple[ResourceLease, ...]:
        """Reclaim every lease the domain rule allows; returns what was dropped."""
        wanted = None if kinds is None else {validate_kind(k) for k in kinds}
        dropped: list[ResourceLease] = []
        with self._connection() as conn:
            rows = [ResourceLease.from_record(json.loads(r[0]))
                    for r in conn.execute("SELECT record FROM resource_leases")]
            for held in rows:
                if wanted is not None and held.kind not in wanted:
                    continue
                if self._try_reclaim(conn, held)[0]:
                    dropped.append(held)
        return tuple(dropped)


def _port_key(key: str) -> int:
    if not (key.isascii() and key.isdigit()):
        raise lease_error("INVALID_INPUT", "a port lease key is the port number")
    return int(key)


def _normal_key(kind: str, key: str) -> tuple[str, str]:
    kind, key = validate_kind(kind), validate_text("key", key)
    if kind == KIND_PORT and key.isascii() and key.isdigit():
        key = str(int(key))
    return kind, key


__all__ = ["DEFAULT_PORT_RANGE", "LOOPBACK_HOST", "SqliteResourceLeaseRegistry", "port_is_free"]
