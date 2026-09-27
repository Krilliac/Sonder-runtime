"""Ports for the host developer-tool inventory.

Discovery and persistence are adapter concerns; the application service and
other lanes (structured test runs) depend only on these protocols.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from ...domain.host_tools.model import InventorySnapshot, ToolRecord


@runtime_checkable
class HostToolLookup(Protocol):
    def lookup(self, name: str) -> ToolRecord | None:
        """Return a record whose path passed the host executable guard."""


class InventoryDiscovery(Protocol):
    def discover(self, *, previous: InventorySnapshot | None, full: bool) -> InventorySnapshot:
        """Discover host tools; reuse ``previous`` versions unless ``full``."""


class InventorySnapshotStore(Protocol):
    def load(self) -> InventorySnapshot | None:
        """Return a validated persisted snapshot, or None (never raises)."""

    def save(self, snapshot: InventorySnapshot) -> None:
        """Atomically persist a snapshot with private permissions."""


__all__ = ["HostToolLookup", "InventoryDiscovery", "InventorySnapshotStore"]
