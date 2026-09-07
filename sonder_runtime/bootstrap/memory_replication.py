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
import hashlib
import hmac
import json
import os
from pathlib import Path
from threading import RLock
from typing import Any
import uuid

from sonder_runtime.adapters.filesystem.atomic_json import file_lock
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


_STATE_FILE_NAME = "memory-replication-state.json"
_STATE_SCHEMA_VERSION = 1
_STATE_MAX_BYTES = 16 * 1024
_STATE_LOCK_TIMEOUT_SECONDS = 1.0
_STATE_MAX_INTEGER = (1 << 63) - 1
_STATE_FAILURE_REASONS = frozenset({
    "sink_failure",
    "sink_identity_changed",
    "invalid_receipt",
    "receipt_identity_mismatch",
    "receipt_source_mismatch",
    "receipt_epoch_mismatch",
    "receipt_sequence_mismatch",
    "receipt_digest_mismatch",
    "receipt_not_durable",
    "receipt_inserted_count_mismatch",
    "source_unavailable",
})


class _ReplicationStateError(RuntimeError):
    """A bounded, non-disclosing local-state failure."""

    def __init__(self, state: str) -> None:
        super().__init__(state)
        self.state = state


def _canonical_state_bytes(payload: dict[str, object]) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")


def _is_state_integer(value: object, *, minimum: int = 0) -> bool:
    return (
        type(value) is int
        and minimum <= value <= _STATE_MAX_INTEGER
    )


def _state_path_for_config(config: SonderConfig) -> Path:
    """Resolve the private state file only after local service start."""
    state_home = config.state.home
    if type(state_home) is str and state_home:
        return Path(state_home) / _STATE_FILE_NAME
    from sonder_runtime.platform import paths as runtime_paths

    return Path(runtime_paths.state_path(_STATE_FILE_NAME))


def _state_integrity_tag(config: SonderConfig, body: dict[str, object]) -> str:
    key = config.secrets.memory_replication_key
    # The typed configuration boundary has already proved this is an exact
    # printable builtin string.  The tag is a tamper detector, never a stored
    # credential or an external authorization token.
    return hmac.new(
        key.encode("ascii"), _canonical_state_bytes(body), hashlib.sha256,
    ).hexdigest()


def _private_state_file_is_safe(path: Path) -> bool:
    if os.name != "posix":
        return True
    try:
        return path.stat().st_mode & 0o077 == 0
    except OSError:
        return False


def _read_state_bytes(path: Path) -> bytes | None:
    try:
        if not path.exists():
            return None
        if not path.is_file() or not _private_state_file_is_safe(path):
            raise _ReplicationStateError("unavailable")
        with path.open("rb") as handle:
            raw = handle.read(_STATE_MAX_BYTES + 1)
    except _ReplicationStateError:
        raise
    except OSError as exc:
        raise _ReplicationStateError("unavailable") from exc
    if not raw or len(raw) > _STATE_MAX_BYTES:
        raise _ReplicationStateError("corrupt")
    return raw


def _attempt_to_state(attempt: dict[str, object]) -> dict[str, object]:
    receipts = attempt["durable_receipts"]
    reasons = attempt["failure_reasons"]
    return {
        "status": attempt["status"],
        "source_epoch": attempt["source_epoch"],
        "after_sequence": attempt["after_sequence"],
        "next_sequence": attempt["next_sequence"],
        "durable_receipts": [
            {
                "peer_id": receipt["peer_id"],
                "source_epoch": receipt["source_epoch"],
                "next_sequence": receipt["next_sequence"],
            }
            for receipt in receipts
        ],
        "failed_peer_ids": list(attempt["failed_peer_ids"]),
        "failure_reasons": [
            {"peer_id": peer_id, "reason": reason}
            for peer_id, reason in reasons
        ],
        "inserted_records": attempt["inserted_records"],
    }


def _state_to_attempt(
    raw: object,
    *,
    peer_ids: tuple[str, ...],
    cursor: int,
) -> dict[str, object]:
    if type(raw) is not dict or set(raw) != {
        "status",
        "source_epoch",
        "after_sequence",
        "next_sequence",
        "durable_receipts",
        "failed_peer_ids",
        "failure_reasons",
        "inserted_records",
    }:
        raise _ReplicationStateError("corrupt")
    status = raw["status"]
    source_epoch = raw["source_epoch"]
    after_sequence = raw["after_sequence"]
    next_sequence = raw["next_sequence"]
    inserted_records = raw["inserted_records"]
    if (
        type(status) is not str
        or status not in {"empty", "replicated", "pending", "failed"}
        or not _is_state_integer(source_epoch)
        or not _is_state_integer(after_sequence)
        or not _is_state_integer(next_sequence)
        or next_sequence < after_sequence
        or not _is_state_integer(inserted_records)
    ):
        raise _ReplicationStateError("corrupt")
    receipts = raw["durable_receipts"]
    failed_peer_ids = raw["failed_peer_ids"]
    failure_reasons = raw["failure_reasons"]
    if (
        type(receipts) is not list
        or type(failed_peer_ids) is not list
        or type(failure_reasons) is not list
        or len(receipts) > len(peer_ids)
        or len(failed_peer_ids) > len(peer_ids)
        or len(failure_reasons) > len(peer_ids) + 1
    ):
        raise _ReplicationStateError("corrupt")
    receipt_rows: list[dict[str, object]] = []
    receipt_ids: list[str] = []
    for receipt in receipts:
        if type(receipt) is not dict or set(receipt) != {
            "peer_id", "source_epoch", "next_sequence",
        }:
            raise _ReplicationStateError("corrupt")
        peer_id = receipt["peer_id"]
        receipt_epoch = receipt["source_epoch"]
        receipt_next_sequence = receipt["next_sequence"]
        if (
            type(peer_id) is not str
            or peer_id not in peer_ids
            or peer_id in receipt_ids
            or not _is_state_integer(receipt_epoch, minimum=1)
            or not _is_state_integer(receipt_next_sequence)
            or receipt_epoch != source_epoch
            or receipt_next_sequence != next_sequence
        ):
            raise _ReplicationStateError("corrupt")
        receipt_ids.append(peer_id)
        receipt_rows.append(
            {
                "peer_id": peer_id,
                "source_epoch": receipt_epoch,
                "next_sequence": receipt_next_sequence,
            }
        )
    if (
        any(type(peer_id) is not str or peer_id not in peer_ids
            for peer_id in failed_peer_ids)
        or len(set(failed_peer_ids)) != len(failed_peer_ids)
    ):
        raise _ReplicationStateError("corrupt")
    reason_rows: list[tuple[str, str]] = []
    for item in failure_reasons:
        if type(item) is not dict or set(item) != {"peer_id", "reason"}:
            raise _ReplicationStateError("corrupt")
        peer_id = item["peer_id"]
        reason = item["reason"]
        if (
            type(peer_id) is not str
            or type(reason) is not str
            or reason not in _STATE_FAILURE_REASONS
        ):
            raise _ReplicationStateError("corrupt")
        reason_rows.append((peer_id, reason))
    if status == "failed":
        if (
            source_epoch != 0
            or after_sequence != cursor
            or next_sequence != cursor
            or receipt_rows
            or failed_peer_ids
            or reason_rows != [("source", "source_unavailable")]
        ):
            raise _ReplicationStateError("corrupt")
    elif (
        source_epoch < 1
        or tuple(peer_id for peer_id, _reason in reason_rows)
        != tuple(failed_peer_ids)
        or set(receipt_ids) & set(failed_peer_ids)
    ):
        raise _ReplicationStateError("corrupt")
    elif status == "replicated" and (
        next_sequence <= after_sequence
        or cursor != next_sequence
        or tuple(receipt_ids) != peer_ids
        or failed_peer_ids
        or reason_rows
    ):
        raise _ReplicationStateError("corrupt")
    elif status == "pending" and (
        next_sequence <= after_sequence
        or cursor != after_sequence
        or not failed_peer_ids
    ):
        raise _ReplicationStateError("corrupt")
    elif status == "empty" and (
        cursor != after_sequence
        or next_sequence != cursor
        or receipt_rows
        or failed_peer_ids
        or reason_rows
        or inserted_records != 0
    ):
        raise _ReplicationStateError("corrupt")
    return {
        "status": status,
        "source_epoch": source_epoch,
        "after_sequence": after_sequence,
        "next_sequence": next_sequence,
        "durable_receipt_peer_ids": tuple(receipt_ids),
        "durable_receipts": tuple(receipt_rows),
        "failed_peer_ids": tuple(failed_peer_ids),
        "failure_reasons": tuple(reason_rows),
        "inserted_records": inserted_records,
    }


def _read_persisted_state(
    path: Path,
    *,
    config: SonderConfig,
    peer_ids: tuple[str, ...],
) -> tuple[int, int, dict[str, object]] | None:
    raw_bytes = _read_state_bytes(path)
    if raw_bytes is None:
        return None
    try:
        raw = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise _ReplicationStateError("corrupt") from exc
    if type(raw) is not dict or set(raw) != {
        "schema_version",
        "generation",
        "source_id",
        "project_scope",
        "cursor",
        "last_attempt",
        "integrity",
    }:
        raise _ReplicationStateError("corrupt")
    integrity = raw["integrity"]
    body = {key: value for key, value in raw.items() if key != "integrity"}
    if type(integrity) is not str or len(integrity) != 64:
        raise _ReplicationStateError("corrupt")
    try:
        expected_integrity = _state_integrity_tag(config, body)
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        # Python's permissive JSON parser accepts values such as NaN.  They
        # are not canonical checkpoint evidence and must become a bounded
        # corrupt-state result rather than escape from service start.
        raise _ReplicationStateError("corrupt") from exc
    if not hmac.compare_digest(expected_integrity, integrity):
        raise _ReplicationStateError("incompatible")
    if raw["schema_version"] != _STATE_SCHEMA_VERSION:
        raise _ReplicationStateError("incompatible")
    generation = raw["generation"]
    cursor = raw["cursor"]
    if (
        not _is_state_integer(generation, minimum=1)
        or not _is_state_integer(cursor)
    ):
        raise _ReplicationStateError("corrupt")
    if (
        type(raw["source_id"]) is not str
        or type(raw["project_scope"]) is not str
        or raw["source_id"] != config.memory_replication.local_node_id
        or raw["project_scope"] != config.memory_replication.project_scope
    ):
        raise _ReplicationStateError("incompatible")
    return generation, cursor, _state_to_attempt(
        raw["last_attempt"], peer_ids=peer_ids, cursor=cursor,
    )


def _write_private_state(path: Path, payload: dict[str, object]) -> None:
    encoded = _canonical_state_bytes(payload) + b"\n"
    if not encoded or len(encoded) > _STATE_MAX_BYTES:
        raise _ReplicationStateError("unavailable")
    temporary: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(".%s.%s.tmp" % (path.name, uuid.uuid4().hex))
        descriptor = os.open(
            str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600,
        )
        try:
            view = memoryview(encoded)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("private state write made no progress")
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, path)
        if os.name == "posix":
            directory_descriptor = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
    except _ReplicationStateError:
        raise
    except OSError as exc:
        raise _ReplicationStateError("unavailable") from exc
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass

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
        self._state_path: Path | None = None
        self._state_generation = 0
        self._state_loaded = False
        self._persistence_state = "uninitialized"
        self._state_fault: str | None = None

    @property
    def config(self) -> SonderConfig:
        """Return the immutable typed config held by this local owner."""
        return self._config

    def _require_live(self) -> None:
        if self._closed:
            raise RuntimeError("memory replication service is closed")

    def _state_file_for_operation(self) -> Path:
        if self._state_path is None:
            self._state_path = _state_path_for_config(self._config)
        return self._state_path

    def _peer_ids(self) -> tuple[str, ...]:
        return tuple(peer.node_id for peer in self._section.peers)

    def _restore_persisted_state(self) -> None:
        """Load one compact local checkpoint without opening a peer or journal."""
        if self._state_loaded:
            return
        self._state_loaded = True
        try:
            loaded = _read_persisted_state(
                self._state_file_for_operation(),
                config=self._config,
                peer_ids=self._peer_ids(),
            )
        except _ReplicationStateError as error:
            self._record_state_fault(error.state)
            return
        if loaded is None:
            self._persistence_state = "empty"
            return
        generation, cursor, attempt = loaded
        self._state_generation = generation
        self._cursor = cursor
        self._last_attempt = attempt
        self._persistence_state = "restored"

    def _record_state_fault(self, state: str) -> None:
        """Fail closed without leaving prior receipt evidence as current."""
        self._state_fault = state
        self._persistence_state = state
        self._last_attempt = self._failed_attempt(
            reason="state_unavailable", cursor=self._cursor,
        )

    def _persist_attempt(self, *, cursor: int, attempt: dict[str, object]) -> None:
        """Atomically publish the next local cursor/attempt checkpoint.

        The checkpoint is written before the in-memory cursor moves.  A write
        failure therefore never returns an outcome that claims a restart-safe
        receipt, and a later operator action can safely retry the exact page.
        """
        if self._state_fault is not None:
            raise DependencyUnavailable("memory replication state is unavailable")
        state_path = self._state_file_for_operation()
        if (
            not _is_state_integer(cursor)
            or self._state_generation >= _STATE_MAX_INTEGER
        ):
            self._record_state_fault("unavailable")
            raise DependencyUnavailable("memory replication state is unavailable")
        next_generation = self._state_generation + 1
        body: dict[str, object] = {
            "schema_version": _STATE_SCHEMA_VERSION,
            "generation": next_generation,
            "source_id": self._section.local_node_id,
            "project_scope": self._section.project_scope,
            "cursor": cursor,
            "last_attempt": _attempt_to_state(attempt),
        }
        payload = {
            **body,
            "integrity": _state_integrity_tag(self._config, body),
        }
        try:
            with file_lock(state_path, timeout=_STATE_LOCK_TIMEOUT_SECONDS):
                current = _read_persisted_state(
                    state_path,
                    config=self._config,
                    peer_ids=self._peer_ids(),
                )
                current_generation = 0 if current is None else current[0]
                if current_generation != self._state_generation:
                    raise _ReplicationStateError("changed")
                _write_private_state(state_path, payload)
        except _ReplicationStateError as error:
            self._record_state_fault(error.state)
            raise DependencyUnavailable("memory replication state is unavailable") from None
        except (OSError, RuntimeError, ValueError, TypeError):
            self._record_state_fault("unavailable")
            raise DependencyUnavailable("memory replication state is unavailable") from None
        self._state_generation = next_generation
        self._cursor = cursor
        self._last_attempt = attempt
        self._persistence_state = "persisted"

    def start(self) -> dict[str, object]:
        """Mark the local service ready without opening a peer connection."""
        with self._lock:
            self._require_live()
            self._restore_persisted_state()
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
            if self._state_fault is not None:
                raise DependencyUnavailable("memory replication state is unavailable")
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
                failure = self._failed_attempt(
                    reason="source_unavailable", cursor=self._cursor,
                )
                self._persist_attempt(cursor=self._cursor, attempt=failure)
                raise DependencyUnavailable("memory replication source is unavailable") from None

            attempt = self._attempt_view(outcome)
            next_cursor = (
                outcome.next_sequence
                if outcome.status == "replicated"
                else self._cursor
            )
            self._persist_attempt(cursor=next_cursor, attempt=attempt)
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
                "persistence": {
                    "state": self._persistence_state,
                    "generation": self._state_generation,
                    "restart_safe": self._state_fault is None
                    and self._state_loaded,
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
