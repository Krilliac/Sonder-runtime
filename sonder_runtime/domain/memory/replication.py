"""Provider-neutral authoritative memory replication records.

The journal represents facts, outcomes, and other write-side memory records.
Retrieval indexes are derived data and are intentionally rebuilt from the
latest non-tombstone records.  This module performs validation only; it does
not elect an owner, replicate bytes over a network, or claim consensus.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from types import MappingProxyType
import re
from typing import Mapping


_IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_PROJECT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}\Z")
_KINDS = frozenset({"fact", "interaction", "outcome", "preference", "lesson_decision"})
_OPERATIONS = frozenset({"upsert", "delete"})
_MAX_PAYLOAD_BYTES = 64 * 1024
_MAX_RECORDS = 1024


class MemoryReplicationError(ValueError):
    """Malformed, conflicting, or stale authoritative memory evidence."""


def _identity(value: object, field: str) -> str:
    if not isinstance(value, str) or _IDENTITY.fullmatch(value) is None:
        raise MemoryReplicationError(f"{field} must be a bounded stable identity")
    return value


def _project(value: object) -> str:
    if not isinstance(value, str) or _PROJECT.fullmatch(value) is None:
        raise MemoryReplicationError("project must be a bounded stable scope")
    return value


def _positive(value: object, field: str, maximum: int = (1 << 63) - 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise MemoryReplicationError(f"{field} must be within 1..{maximum}")
    return value


def _timestamp(value: object) -> str:
    if not isinstance(value, str):
        raise MemoryReplicationError("recorded_at must be a timezone-aware timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MemoryReplicationError("recorded_at must be a timezone-aware timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise MemoryReplicationError("recorded_at must be a timezone-aware timestamp")
    return parsed.isoformat()


def _payload(value: object, operation: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise MemoryReplicationError("payload must be a JSON object")
    if operation == "delete" and value:
        raise MemoryReplicationError("tombstone payload must be empty")
    if len(value) > 64:
        raise MemoryReplicationError("payload has too many fields")
    try:
        encoded = json.dumps(
            value, sort_keys=True, separators=(",", ":"),
            ensure_ascii=True, allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError) as exc:
        raise MemoryReplicationError("payload must contain bounded JSON values") from exc
    if len(encoded) > _MAX_PAYLOAD_BYTES:
        raise MemoryReplicationError("payload exceeds the replication bound")
    return MappingProxyType(dict(value))


@dataclass(frozen=True, slots=True)
class MemoryMutation:
    """One immutable, versioned write-side memory mutation."""

    source_id: str
    source_epoch: int
    sequence: int
    entity_kind: str
    entity_id: str
    version: int
    operation: str
    project: str
    payload: Mapping[str, object]
    recorded_at: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_id", _identity(self.source_id, "source_id"))
        object.__setattr__(self, "source_epoch", _positive(self.source_epoch, "source_epoch"))
        object.__setattr__(self, "sequence", _positive(self.sequence, "sequence"))
        if self.entity_kind not in _KINDS:
            raise MemoryReplicationError("entity_kind is not supported")
        object.__setattr__(self, "entity_id", _identity(self.entity_id, "entity_id"))
        object.__setattr__(self, "version", _positive(self.version, "version"))
        if self.operation not in _OPERATIONS:
            raise MemoryReplicationError("operation must be upsert or delete")
        object.__setattr__(self, "project", _project(self.project))
        object.__setattr__(self, "payload", _payload(self.payload, self.operation))
        object.__setattr__(self, "recorded_at", _timestamp(self.recorded_at))

    @property
    def is_tombstone(self) -> bool:
        return self.operation == "delete"

    @property
    def entity_key(self) -> tuple[str, str, str]:
        return self.project, self.entity_kind, self.entity_id

    @property
    def digest(self) -> str:
        encoded = json.dumps(
            {
                "schema": "sonder.memory-mutation.v1",
                "source_id": self.source_id,
                "source_epoch": self.source_epoch,
                "sequence": self.sequence,
                "entity_kind": self.entity_kind,
                "entity_id": self.entity_id,
                "version": self.version,
                "operation": self.operation,
                "project": self.project,
                "payload": dict(self.payload),
                "recorded_at": self.recorded_at,
            },
            sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False,
        ).encode("ascii")
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class MemoryReplicationBatch:
    """A bounded ordered transfer page from one source journal."""

    source_id: str
    source_epoch: int
    after_sequence: int
    records: tuple[MemoryMutation, ...]
    next_sequence: int
    has_more: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_id", _identity(self.source_id, "source_id"))
        object.__setattr__(self, "source_epoch", _positive(self.source_epoch, "source_epoch"))
        if isinstance(self.after_sequence, bool) or not isinstance(self.after_sequence, int) or self.after_sequence < 0:
            raise MemoryReplicationError("after_sequence must be a non-negative integer")
        if type(self.records) is not tuple or len(self.records) > _MAX_RECORDS:
            raise MemoryReplicationError("records must be a bounded tuple")
        previous = self.after_sequence
        for record in self.records:
            if not isinstance(record, MemoryMutation) or record.source_id != self.source_id:
                raise MemoryReplicationError("batch records must belong to the source")
            if record.source_epoch != self.source_epoch:
                raise MemoryReplicationError("batch records must share the source epoch")
            if record.sequence <= previous:
                raise MemoryReplicationError("batch sequences must increase")
            previous = record.sequence
        expected_next = previous if self.records else self.after_sequence
        if self.next_sequence != expected_next:
            raise MemoryReplicationError("next_sequence must identify the last record")
        if type(self.has_more) is not bool:
            raise MemoryReplicationError("has_more must be boolean")


__all__ = ["MemoryMutation", "MemoryReplicationBatch", "MemoryReplicationError"]
