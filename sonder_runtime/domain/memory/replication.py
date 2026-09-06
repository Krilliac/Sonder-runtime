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


def _non_negative(value: object, field: str, maximum: int = (1 << 63) - 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        raise MemoryReplicationError(f"{field} must be within 0..{maximum}")
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


def _canonical_json(value: Mapping[str, object]) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError) as exc:
        raise MemoryReplicationError("replication evidence must be canonical JSON") from exc


def _require_wire_keys(value: object, expected: frozenset[str], label: str) -> dict[str, object]:
    if not isinstance(value, dict) or frozenset(value) != expected:
        raise MemoryReplicationError(f"{label} has an invalid wire shape")
    return value


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
        if not isinstance(self.entity_kind, str) or self.entity_kind not in _KINDS:
            raise MemoryReplicationError("entity_kind is not supported")
        object.__setattr__(self, "entity_id", _identity(self.entity_id, "entity_id"))
        object.__setattr__(self, "version", _positive(self.version, "version"))
        if not isinstance(self.operation, str) or self.operation not in _OPERATIONS:
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
        return hashlib.sha256(_canonical_json(self._wire_fields())).hexdigest()

    def _wire_fields(self) -> dict[str, object]:
        return {
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
        }

    def as_dict(self) -> dict[str, object]:
        """Return the canonical, self-authenticating wire representation."""
        value = self._wire_fields()
        value["digest"] = self.digest
        return value

    @classmethod
    def from_dict(cls, value: object) -> "MemoryMutation":
        expected = frozenset({
            "schema", "source_id", "source_epoch", "sequence", "entity_kind",
            "entity_id", "version", "operation", "project", "payload",
            "recorded_at", "digest",
        })
        wire = _require_wire_keys(value, expected, "memory mutation")
        if wire["schema"] != "sonder.memory-mutation.v1":
            raise MemoryReplicationError("memory mutation schema is unsupported")
        supplied_digest = wire["digest"]
        if not isinstance(supplied_digest, str) or _DIGEST.fullmatch(supplied_digest) is None:
            raise MemoryReplicationError("memory mutation digest is invalid")
        mutation = cls(
            source_id=wire["source_id"],
            source_epoch=wire["source_epoch"],
            sequence=wire["sequence"],
            entity_kind=wire["entity_kind"],
            entity_id=wire["entity_id"],
            version=wire["version"],
            operation=wire["operation"],
            project=wire["project"],
            payload=wire["payload"],
            recorded_at=wire["recorded_at"],
        )
        if mutation.digest != supplied_digest:
            raise MemoryReplicationError("memory mutation digest does not match fields")
        return mutation


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
        object.__setattr__(self, "after_sequence", _non_negative(self.after_sequence, "after_sequence"))
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
        object.__setattr__(self, "next_sequence", _non_negative(self.next_sequence, "next_sequence"))
        if self.next_sequence != expected_next:
            raise MemoryReplicationError("next_sequence must identify the last record")
        if type(self.has_more) is not bool:
            raise MemoryReplicationError("has_more must be boolean")

    def _wire_fields(self) -> dict[str, object]:
        return {
            "schema": "sonder.memory-replication-batch.v1",
            "source_id": self.source_id,
            "source_epoch": self.source_epoch,
            "after_sequence": self.after_sequence,
            "records": [record.as_dict() for record in self.records],
            "next_sequence": self.next_sequence,
            "has_more": self.has_more,
        }

    @property
    def digest(self) -> str:
        return hashlib.sha256(_canonical_json(self._wire_fields())).hexdigest()

    def as_dict(self) -> dict[str, object]:
        """Return the canonical, self-authenticating wire representation."""
        value = self._wire_fields()
        value["digest"] = self.digest
        return value

    @classmethod
    def from_dict(cls, value: object) -> "MemoryReplicationBatch":
        expected = frozenset({
            "schema", "source_id", "source_epoch", "after_sequence", "records",
            "next_sequence", "has_more", "digest",
        })
        wire = _require_wire_keys(value, expected, "memory replication batch")
        if wire["schema"] != "sonder.memory-replication-batch.v1":
            raise MemoryReplicationError("memory replication batch schema is unsupported")
        supplied_digest = wire["digest"]
        if not isinstance(supplied_digest, str) or _DIGEST.fullmatch(supplied_digest) is None:
            raise MemoryReplicationError("memory replication batch digest is invalid")
        records_value = wire["records"]
        if not isinstance(records_value, list):
            raise MemoryReplicationError("memory replication batch records must be a list")
        batch = cls(
            source_id=wire["source_id"],
            source_epoch=wire["source_epoch"],
            after_sequence=wire["after_sequence"],
            records=tuple(MemoryMutation.from_dict(item) for item in records_value),
            next_sequence=wire["next_sequence"],
            has_more=wire["has_more"],
        )
        if batch.digest != supplied_digest:
            raise MemoryReplicationError("memory replication batch digest does not match fields")
        return batch


@dataclass(frozen=True, slots=True)
class MemoryReplicaReceipt:
    """Durability evidence returned by one explicitly configured replica."""

    replica_id: str
    source_id: str
    source_epoch: int
    next_sequence: int
    batch_digest: str
    durable: bool
    inserted_records: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "replica_id", _identity(self.replica_id, "replica_id"))
        object.__setattr__(self, "source_id", _identity(self.source_id, "source_id"))
        object.__setattr__(self, "source_epoch", _positive(self.source_epoch, "source_epoch"))
        object.__setattr__(self, "next_sequence", _non_negative(self.next_sequence, "next_sequence"))
        if not isinstance(self.batch_digest, str) or _DIGEST.fullmatch(self.batch_digest) is None:
            raise MemoryReplicationError("batch_digest must be a SHA-256 digest")
        if type(self.durable) is not bool:
            raise MemoryReplicationError("durable must be boolean")
        object.__setattr__(
            self,
            "inserted_records",
            _non_negative(self.inserted_records, "inserted_records", _MAX_RECORDS),
        )

    def _wire_fields(self) -> dict[str, object]:
        return {
            "schema": "sonder.memory-replica-receipt.v1",
            "replica_id": self.replica_id,
            "source_id": self.source_id,
            "source_epoch": self.source_epoch,
            "next_sequence": self.next_sequence,
            "batch_digest": self.batch_digest,
            "durable": self.durable,
            "inserted_records": self.inserted_records,
        }

    @property
    def digest(self) -> str:
        """Return a canonical digest for transport response validation."""

        return hashlib.sha256(_canonical_json(self._wire_fields())).hexdigest()

    def as_dict(self) -> dict[str, object]:
        """Return a self-authenticating, bounded receipt representation."""

        value = self._wire_fields()
        value["digest"] = self.digest
        return value

    @classmethod
    def from_dict(cls, value: object) -> "MemoryReplicaReceipt":
        expected = frozenset({
            "schema", "replica_id", "source_id", "source_epoch",
            "next_sequence", "batch_digest", "durable", "inserted_records",
            "digest",
        })
        wire = _require_wire_keys(value, expected, "memory replica receipt")
        if wire["schema"] != "sonder.memory-replica-receipt.v1":
            raise MemoryReplicationError("memory replica receipt schema is unsupported")
        supplied_digest = wire["digest"]
        if not isinstance(supplied_digest, str) or _DIGEST.fullmatch(supplied_digest) is None:
            raise MemoryReplicationError("memory replica receipt digest is invalid")
        receipt = cls(
            replica_id=wire["replica_id"],
            source_id=wire["source_id"],
            source_epoch=wire["source_epoch"],
            next_sequence=wire["next_sequence"],
            batch_digest=wire["batch_digest"],
            durable=wire["durable"],
            inserted_records=wire["inserted_records"],
        )
        if receipt.digest != supplied_digest:
            raise MemoryReplicationError(
                "memory replica receipt digest does not match fields"
            )
        return receipt


__all__ = [
    "MemoryMutation",
    "MemoryReplicationBatch",
    "MemoryReplicaReceipt",
    "MemoryReplicationError",
]
