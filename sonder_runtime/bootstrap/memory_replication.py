"""Owned, explicit fact-only trusted-peer replication composition.

This module is deliberately a lifecycle boundary rather than a delivery loop.
It builds no peer client until an operator calls :meth:`replicate_once`, and
it never schedules a retry, discovers a member, changes ownership, or treats a
receipt as takeover evidence.  The only replicated records are the explicit
project-scoped ``fact`` mutations admitted by the authoritative source and
fact-projecting receiver contracts.
"""
from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from threading import RLock
from typing import Any

from sonder_runtime.application.memory.replication import (
    MemoryReplicationCoordinator,
    MemoryReplicationReceiver,
    MemoryReplicationSink,
)
from sonder_runtime.domain.common.errors import DependencyUnavailable
from sonder_runtime.platform.config import ConfigError, SonderConfig
from sonder_runtime.platform.memory_replication_config import (
    MemoryReplicationPeerConfig,
    memory_replication_errors,
)


def _default_database_path(config: SonderConfig) -> Path:
    """Resolve the normal memory database only at an explicit operation."""
    state_home = config.state.home
    if type(state_home) is str and state_home:
        return Path(state_home) / "memory.db"
    # The compatibility root has already applied the typed home before it
    # performs a service operation.  ``memory_db_path`` retains its existing
    # compatibility/migration behavior, but must never run at construction or
    # ordinary status time.
    from sonder_runtime.platform import paths as runtime_paths

    return Path(runtime_paths.memory_db_path())


def _default_journal_factory(
    *, database_path: Path, source_id: str, project_scope: str,
):
    from sonder_runtime.adapters.persistence.sqlite.memory_replication import (
        SQLiteMemoryReplicationJournal,
    )

    return SQLiteMemoryReplicationJournal(
        database_path, source_id=source_id, project_scope=project_scope,
    )


def _default_sink_factory(
    *, peer: MemoryReplicationPeerConfig, config: SonderConfig,
) -> MemoryReplicationSink:
    from sonder_runtime.adapters.memory_replication.http_client import (
        HttpsMemoryReplicationSink,
    )

    section = config.memory_replication
    return HttpsMemoryReplicationSink(
        identity=peer.node_id,
        origin=peer.origin,
        api_key=config.secrets.memory_replication_key,
        timeout_seconds=section.request_timeout_seconds,
        max_request_bytes=section.max_request_bytes,
        max_response_bytes=section.max_response_bytes,
    )


def _default_connection_factory(database_path: Path):
    from sonder_runtime.adapters.memory_store import connect

    # A ThreadingHTTPServer may invoke the receiver from distinct request
    # threads.  The serialized sink below owns the single connection access.
    return connect(database_path, check_same_thread=False)


class _UnavailablePeerSink:
    """Preserve a configured peer identity when client construction fails."""

    def __init__(self, identity: str) -> None:
        self.identity = identity

    def apply(self, _batch):
        raise DependencyUnavailable("configured peer client is unavailable")


class _SerializedFactSink:
    """Serialize receiver projection over its owned local SQLite connection."""

    def __init__(self, sink, connection) -> None:
        self.identity = sink.identity
        self.connection = connection
        self._sink = sink
        self._lock = RLock()

    def apply(self, batch):
        with self._lock:
            return self._sink.apply(batch)

    def close(self) -> None:
        with self._lock:
            self.connection.close()


class MemoryReplicationService:
    """One explicit, bounded fact-replication owner for a typed config.

    Construction records only validated local policy.  ``start`` is a local
    lifecycle transition, and every outgoing attempt must be explicitly made
    through ``replicate_once``.  The service intentionally keeps one local
    cursor: it advances only after every configured fixed peer returns a
    matching durable receipt, so a stopped peer is retried only by a later
    explicit call with the same page.
    """

    def __init__(
        self,
        config: SonderConfig,
        *,
        database_path: str | Path | None = None,
        journal_factory: Callable[..., Any] = _default_journal_factory,
        sink_factory: Callable[..., MemoryReplicationSink] = _default_sink_factory,
        connection_factory: Callable[[Path], Any] = _default_connection_factory,
    ) -> None:
        if type(config) is not SonderConfig:
            raise TypeError("memory replication requires an exact typed SonderConfig")
        errors = memory_replication_errors(config)
        if errors:
            raise ConfigError(errors)
        if config.memory_replication.enabled is not True:
            raise ConfigError(["memory replication service requires enabled=true"])
        if not callable(journal_factory) or not callable(sink_factory):
            raise TypeError("memory replication factories must be callable")
        if not callable(connection_factory):
            raise TypeError("memory replication connection factory must be callable")

        self._config = config
        self._section = config.memory_replication
        self._database_path = None if database_path is None else Path(database_path)
        self._journal_factory = journal_factory
        self._sink_factory = sink_factory
        self._connection_factory = connection_factory
        self._lock = RLock()
        self._started = False
        self._closed = False
        self._journal = None
        self._receiver = None
        self._receiver_sink = None
        self._cursor = 0
        self._last_attempt: dict[str, object] | None = None

    @property
    def config(self) -> SonderConfig:
        """Return the immutable typed config held by this local owner."""
        return self._config

    def _require_live(self) -> None:
        if self._closed:
            raise RuntimeError("memory replication service is closed")

    def start(self) -> dict[str, object]:
        """Mark the local service ready without opening a peer connection."""
        with self._lock:
            self._require_live()
            self._started = True
            return self.status()

    def _journal_for_attempt(self):
        if self._journal is None:
            self._journal = self._journal_factory(
                database_path=self._database_path_for_operation(),
                source_id=self._section.local_node_id,
                project_scope=self._section.project_scope,
            )
        return self._journal

    def _database_path_for_operation(self) -> Path:
        if self._database_path is None:
            self._database_path = _default_database_path(self._config)
        return self._database_path

    def _sinks_for_attempt(self) -> tuple[MemoryReplicationSink, ...]:
        """Construct exactly the configured peers for this one explicit call."""
        sinks: list[MemoryReplicationSink] = []
        for peer in self._section.peers:
            try:
                sink = self._sink_factory(peer=peer, config=self._config)
            except Exception:
                sink = _UnavailablePeerSink(peer.node_id)
            try:
                identity = getattr(sink, "identity", None)
            except Exception:
                identity = None
            if type(identity) is not str or identity != peer.node_id:
                # The factory is an internal composition seam, never peer
                # selection authority.  A mismatch stays visible as a failure
                # for the configured identity instead of changing admission.
                sink = _UnavailablePeerSink(peer.node_id)
            sinks.append(sink)
        return tuple(sinks)

    @staticmethod
    def _failed_attempt(*, reason: str, cursor: int) -> dict[str, object]:
        return {
            "status": "failed",
            "source_epoch": 0,
            "after_sequence": cursor,
            "next_sequence": cursor,
            "durable_receipt_peer_ids": (),
            "durable_receipts": (),
            "failed_peer_ids": (),
            "failure_reasons": (("source", reason),),
            "inserted_records": 0,
        }

    @staticmethod
    def _attempt_view(outcome) -> dict[str, object]:
        durable_peer_ids = tuple(
            peer_id for peer_id in outcome.replica_ids
            if peer_id != outcome.source_id
        )
        return {
            "status": outcome.status,
            "source_epoch": outcome.source_epoch,
            "after_sequence": outcome.after_sequence,
            "next_sequence": outcome.next_sequence,
            "durable_receipt_peer_ids": durable_peer_ids,
            "durable_receipts": tuple(
                {
                    "peer_id": peer_id,
                    "source_epoch": outcome.source_epoch,
                    "next_sequence": outcome.next_sequence,
                }
                for peer_id in durable_peer_ids
            ),
            "failed_peer_ids": outcome.failed_replica_ids,
            "failure_reasons": outcome.failure_reasons,
            "inserted_records": outcome.inserted_records,
        }

    def replicate_once(self):
        """Send one bounded current page to every fixed configured peer.

        There is no caller-selected peer, source scope, cursor, retry policy,
        or background dispatch.  A stopped peer returns the coordinator's
        stable ``pending``/``sink_failure`` evidence and leaves the local cursor
        untouched for a later operator-invoked exact retry.
        """
        with self._lock:
            self._require_live()
            if not self._started:
                raise RuntimeError("memory replication service must be started")
            try:
                journal = self._journal_for_attempt()
                coordinator = MemoryReplicationCoordinator(
                    journal,
                    self._sinks_for_attempt(),
                    # This is not quorum inference: all fixed configured peers
                    # must acknowledge the page before the local cursor moves.
                    minimum_data_replicas=1 + len(self._section.peers),
                    limit=self._section.max_batch_records,
                    project=self._section.project_scope,
                )
                outcome = coordinator.replicate(after_sequence=self._cursor)
            except Exception:
                self._last_attempt = self._failed_attempt(
                    reason="source_unavailable", cursor=self._cursor,
                )
                raise DependencyUnavailable("memory replication source is unavailable") from None

            self._last_attempt = self._attempt_view(outcome)
            if outcome.status == "replicated":
                self._cursor = outcome.next_sequence
            return outcome

    def receiver(self) -> MemoryReplicationReceiver | None:
        """Create the optional fixed-policy local receiver without peer I/O."""
        with self._lock:
            self._require_live()
            if not self._started:
                raise RuntimeError("memory replication service must be started")
            if self._section.receiver_enabled is not True:
                return None
            if self._receiver is not None:
                return self._receiver
            connection = self._connection_factory(self._database_path_for_operation())
            try:
                from sonder_runtime.adapters.persistence.sqlite.memory_replication import (
                    SQLiteFactReplicationSink,
                )

                sink = _SerializedFactSink(
                    SQLiteFactReplicationSink(
                        self._section.local_node_id,
                        connection,
                        project_scope=self._section.project_scope,
                        max_records=self._section.max_batch_records,
                    ),
                    connection,
                )
                receiver = MemoryReplicationReceiver(
                    sink,
                    api_key=self._config.secrets.memory_replication_key,
                    accepted_source_ids=self._section.accepted_source_ids,
                    max_body_bytes=self._section.max_request_bytes,
                )
            except BaseException:
                try:
                    connection.close()
                except Exception:
                    pass
                raise
            self._receiver_sink = sink
            self._receiver = receiver
            return receiver

    def status(self) -> dict[str, object]:
        """Return local policy and bounded last-attempt evidence only."""
        with self._lock:
            return {
                "enabled": True,
                "started": self._started,
                "closed": self._closed,
                "transport": "bounded_authenticated_fact_replication",
                "fact_only": True,
                "configured_peer_ids": tuple(
                    peer.node_id for peer in self._section.peers
                ),
                "receiver_configured": self._receiver is not None,
                "journal": {
                    "opened": self._journal is not None,
                    "source_id": self._section.local_node_id,
                    "project_scope": self._section.project_scope,
                    "cursor": self._cursor,
                },
                "last_attempt": (
                    None
                    if self._last_attempt is None
                    else deepcopy(self._last_attempt)
                ),
                "background_retry": False,
                "peer_discovery": False,
                "automatic_takeover_available": False,
                "automatic_failback_available": False,
                "automatic_memory_migration_available": False,
            }

    def close(self) -> None:
        """Close local handles only; close never probes or infers recovery."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            receiver_sink, self._receiver_sink = self._receiver_sink, None
            self._receiver = None
            journal, self._journal = self._journal, None
            try:
                if receiver_sink is not None:
                    receiver_sink.close()
            finally:
                if journal is not None:
                    close = getattr(journal, "close", None)
                    if callable(close):
                        close()


def compose_memory_replication_service(
    config: SonderConfig,
    *,
    database_path: str | Path | None = None,
    journal_factory: Callable[..., Any] = _default_journal_factory,
    sink_factory: Callable[..., MemoryReplicationSink] = _default_sink_factory,
    connection_factory: Callable[[Path], Any] = _default_connection_factory,
) -> MemoryReplicationService | None:
    """Return an owner only for a valid enabled typed configuration."""
    if type(config) is not SonderConfig:
        raise TypeError("memory replication requires an exact typed SonderConfig")
    errors = memory_replication_errors(config)
    if errors:
        raise ConfigError(errors)
    if config.memory_replication.enabled is not True:
        return None
    return MemoryReplicationService(
        config,
        database_path=database_path,
        journal_factory=journal_factory,
        sink_factory=sink_factory,
        connection_factory=connection_factory,
    )


__all__ = ["MemoryReplicationService", "compose_memory_replication_service"]
