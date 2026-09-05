"""Bounded scheduler ownership groups and deterministic session routing.

The router is a pure, in-process partition map.  It gives callers stable
ownership-group selection, paginated inventory, and explicit protocol
negotiation.  It does not perform discovery, replication, or failover; those
operations remain adapter responsibilities with their own evidence gates.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
from typing import Iterable


_IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_STATUSES = frozenset({"active", "draining", "paused"})
MAX_PARTITIONS = 4096
MAX_PAGE_SIZE = 128


class PartitionRoutingError(ValueError):
    """Invalid partition metadata or an unavailable routing request."""


def _identity(value: object, field: str) -> str:
    if not isinstance(value, str) or not _IDENTITY.fullmatch(value):
        raise PartitionRoutingError(f"{field} must be a bounded stable identity")
    return value


@dataclass(frozen=True, slots=True)
class PartitionDescriptor:
    """One scheduler ownership group and its admitted capacity weight."""

    partition_id: str
    owner_id: str
    capacity: int = 1
    revision: int = 1
    status: str = "active"

    def __post_init__(self) -> None:
        object.__setattr__(self, "partition_id", _identity(self.partition_id, "partition_id"))
        object.__setattr__(self, "owner_id", _identity(self.owner_id, "owner_id"))
        if type(self.capacity) is not int or not 1 <= self.capacity <= 1 << 20:
            raise PartitionRoutingError("capacity must be an integer within 1..2^20")
        if type(self.revision) is not int or not 1 <= self.revision <= (1 << 63) - 1:
            raise PartitionRoutingError("revision must be a positive bounded integer")
        if self.status not in _STATUSES:
            raise PartitionRoutingError("status must be active, draining, or paused")


@dataclass(frozen=True, slots=True)
class PartitionPage:
    items: tuple[PartitionDescriptor, ...]
    next_cursor: str | None
    complete: bool


@dataclass(frozen=True, slots=True)
class ProtocolDecision:
    accepted: bool
    selected_version: int
    reason: str


class PartitionRouter:
    """Deterministic bounded map from session keys to active partitions."""

    def __init__(
        self,
        partitions: Iterable[PartitionDescriptor] = (),
        *,
        protocol_version: int = 1,
        max_partitions: int = MAX_PARTITIONS,
    ) -> None:
        if type(protocol_version) is not int or not 1 <= protocol_version <= 16:
            raise PartitionRoutingError("protocol_version must be within 1..16")
        if type(max_partitions) is not int or not 1 <= max_partitions <= MAX_PARTITIONS:
            raise PartitionRoutingError("max_partitions is outside the supported bound")
        self.protocol_version = protocol_version
        self.max_partitions = max_partitions
        self._partitions: dict[str, PartitionDescriptor] = {}
        for descriptor in tuple(partitions):
            self._insert(descriptor)

    def _insert(self, descriptor: PartitionDescriptor) -> None:
        if not isinstance(descriptor, PartitionDescriptor):
            raise PartitionRoutingError("partitions must contain descriptors")
        if descriptor.partition_id in self._partitions:
            raise PartitionRoutingError("duplicate partition_id")
        if len(self._partitions) >= self.max_partitions:
            raise PartitionRoutingError("partition inventory capacity exhausted")
        self._partitions[descriptor.partition_id] = descriptor

    def partition(self, partition_id: str) -> PartitionDescriptor:
        key = _identity(partition_id, "partition_id")
        try:
            return self._partitions[key]
        except KeyError as exc:
            raise PartitionRoutingError("partition is not enrolled") from exc

    @property
    def partitions(self) -> tuple[PartitionDescriptor, ...]:
        return tuple(self._partitions[key] for key in sorted(self._partitions))

    @property
    def active_partitions(self) -> tuple[PartitionDescriptor, ...]:
        return tuple(item for item in self.partitions if item.status == "active")

    def upsert(self, descriptor: PartitionDescriptor) -> None:
        if not isinstance(descriptor, PartitionDescriptor):
            raise PartitionRoutingError("partition descriptor is required")
        current = self._partitions.get(descriptor.partition_id)
        if current is None:
            self._insert(descriptor)
            return
        if descriptor.revision < current.revision:
            raise PartitionRoutingError("partition revision must be monotonic")
        if descriptor.revision == current.revision and descriptor != current:
            raise PartitionRoutingError("partition revision conflicts with existing metadata")
        self._partitions[descriptor.partition_id] = descriptor

    def remove(self, partition_id: str, *, expected_revision: int | None = None) -> bool:
        current = self.partition(partition_id)
        if expected_revision is not None and expected_revision != current.revision:
            raise PartitionRoutingError("partition revision is stale")
        del self._partitions[current.partition_id]
        return True

    def route(self, key: str) -> PartitionDescriptor:
        if not isinstance(key, str) or not 1 <= len(key.encode("utf-8")) <= 4096:
            raise PartitionRoutingError("routing key must be bounded text")
        active = self.active_partitions
        if not active:
            raise PartitionRoutingError("no active scheduler partition")

        def score(item: PartitionDescriptor) -> tuple[int, str]:
            digest = hashlib.sha256(
                (key + "\0" + item.partition_id).encode("utf-8")
            ).digest()
            # Capacity is a scheduling weight, not a promise of free capacity.
            return int.from_bytes(digest[:16], "big") * item.capacity, item.partition_id

        return max(active, key=score)

    def page(self, *, after: str | None = None, limit: int = 50) -> PartitionPage:
        if type(limit) is not int or not 1 <= limit <= MAX_PAGE_SIZE:
            raise PartitionRoutingError(f"limit must be an integer within 1..{MAX_PAGE_SIZE}")
        records = self.partitions
        start = 0
        if after is not None:
            cursor = _identity(after, "partition cursor")
            ids = [item.partition_id for item in records]
            if cursor not in ids:
                raise PartitionRoutingError("partition cursor is unknown")
            start = ids.index(cursor) + 1
        items = records[start:start + limit]
        complete = start + limit >= len(records)
        return PartitionPage(
            items=items,
            next_cursor=None if complete else items[-1].partition_id,
            complete=complete,
        )

    def negotiate(self, client_version: int) -> ProtocolDecision:
        if type(client_version) is not int or not 1 <= client_version <= 16:
            return ProtocolDecision(False, self.protocol_version, "protocol_version_invalid")
        if client_version != self.protocol_version:
            return ProtocolDecision(False, self.protocol_version, "protocol_version_mismatch")
        return ProtocolDecision(True, self.protocol_version, "protocol_version_accepted")


__all__ = [
    "MAX_PAGE_SIZE",
    "MAX_PARTITIONS",
    "PartitionDescriptor",
    "PartitionPage",
    "PartitionRouter",
    "PartitionRoutingError",
    "ProtocolDecision",
]
