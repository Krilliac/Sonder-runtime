"""Transaction-neutral persistence primitives for cross-cutting state.

This module deliberately stops at an application persistence port.  It does
not choose SQLite, SQL, a queue, or a transaction manager.  Adapters can use
the same record and outbox shapes while keeping the atomic record/CAS/outbox
write as their storage concern.
"""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from threading import Lock
from types import MappingProxyType
from typing import Protocol


CURRENT_SCHEMA_VERSION = 1


class PersistenceError(ValueError):
    """Base error for invalid persistence-neutral values."""


class UnsupportedSchemaVersion(PersistenceError):
    """Raised when data was written by a newer, incompatible schema."""


class RevisionConflict(PersistenceError):
    """Raised when a caller explicitly requests strict CAS semantics."""


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in deepcopy(dict(value)).items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in deepcopy(value))
    if isinstance(value, set):
        return frozenset(_freeze(item) for item in deepcopy(value))
    return value


def _immutable_mapping(value: Mapping[str, object], field_name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise PersistenceError(f"{field_name} must be a mapping")
    return _freeze(value)  # type: ignore[return-value]


def _validate_schema(version: int) -> None:
    if not isinstance(version, int) or version < 1:
        raise PersistenceError("schema_version must be a positive integer")
    if version > CURRENT_SCHEMA_VERSION:
        raise UnsupportedSchemaVersion(
            f"schema_version {version} is newer than supported version {CURRENT_SCHEMA_VERSION}"
        )


@dataclass(frozen=True)
class TransactionNeutralRecord:
    """Versioned aggregate state with a monotonic revision.

    The payload is copied and exposed read-only at the application boundary;
    transaction and serialization details remain outside this domain-owned
    shape.
    """

    aggregate_id: str
    revision: int
    payload: Mapping[str, object]
    schema_version: int = CURRENT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.aggregate_id, str) or not self.aggregate_id.strip():
            raise PersistenceError("aggregate_id must be non-empty")
        if not isinstance(self.revision, int) or self.revision < 0:
            raise PersistenceError("revision must be a non-negative integer")
        _validate_schema(self.schema_version)
        object.__setattr__(self, "payload", _immutable_mapping(self.payload, "payload"))


@dataclass(frozen=True)
class OutboxEvent:
    """Immutable event staged with a record revision for later dispatch."""

    event_id: str
    aggregate_id: str
    event_type: str
    revision: int
    payload: Mapping[str, object]
    occurred_at: str
    schema_version: int = CURRENT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        for name in ("event_id", "aggregate_id", "event_type", "occurred_at"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name).strip():
                raise PersistenceError(f"{name} must be non-empty")
        if not isinstance(self.revision, int) or self.revision < 0:
            raise PersistenceError("revision must be a non-negative integer")
        _validate_schema(self.schema_version)
        object.__setattr__(self, "payload", _immutable_mapping(self.payload, "payload"))


class OutboxCASRepository(Protocol):
    """Persistence port for atomic aggregate revision and outbox staging."""

    def get(self, aggregate_id: str) -> TransactionNeutralRecord | None: ...

    def append(
        self,
        record: TransactionNeutralRecord,
        event: OutboxEvent,
        *,
        expected_revision: int,
    ) -> TransactionNeutralRecord | None: ...

    def outbox(self) -> tuple[OutboxEvent, ...]: ...


class InMemoryOutboxCASRepository:
    """Thread-safe reference adapter; production adapters own durability."""

    def __init__(self) -> None:
        self._records: dict[str, TransactionNeutralRecord] = {}
        self._events: list[OutboxEvent] = []
        self._lock = Lock()

    def get(self, aggregate_id: str) -> TransactionNeutralRecord | None:
        with self._lock:
            return self._records.get(aggregate_id)

    def append(
        self,
        record: TransactionNeutralRecord,
        event: OutboxEvent,
        *,
        expected_revision: int,
    ) -> TransactionNeutralRecord | None:
        if record.aggregate_id != event.aggregate_id or record.revision != event.revision:
            raise PersistenceError("record and event identity/revision must match")
        with self._lock:
            current = self._records.get(record.aggregate_id)
            current_revision = current.revision if current is not None else -1
            if current_revision != expected_revision or record.revision != expected_revision + 1:
                return None
            if any(existing.event_id == event.event_id for existing in self._events):
                raise PersistenceError("event_id already exists")
            self._records[record.aggregate_id] = record
            self._events.append(event)
            return record

    def outbox(self) -> tuple[OutboxEvent, ...]:
        with self._lock:
            return tuple(self._events)


__all__ = [
    "CURRENT_SCHEMA_VERSION", "InMemoryOutboxCASRepository", "OutboxCASRepository",
    "OutboxEvent", "PersistenceError", "RevisionConflict", "TransactionNeutralRecord",
    "UnsupportedSchemaVersion",
]
