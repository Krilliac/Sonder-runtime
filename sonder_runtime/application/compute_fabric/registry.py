"""Configured compute-node authority and last-observation registry."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from threading import RLock

from ...domain.compute_fabric import ComputeNode, NodeSnapshot


def _utc(value: datetime, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return value.astimezone(timezone.utc)


class ComputeNodeRegistry:
    """Keep configured authority separate from independently observed state."""

    def __init__(
        self,
        nodes: tuple[ComputeNode, ...],
        *,
        snapshot_ttl: timedelta = timedelta(seconds=30),
    ) -> None:
        if not isinstance(snapshot_ttl, timedelta) or not timedelta(0) < snapshot_ttl <= timedelta(hours=1):
            raise ValueError("snapshot_ttl must be within zero and one hour")
        configured: dict[str, ComputeNode] = {}
        for node in nodes:
            if not isinstance(node, ComputeNode):
                raise TypeError("nodes must contain ComputeNode values")
            if node.node_id in configured:
                raise ValueError(f"duplicate configured node identity: {node.node_id}")
            configured[node.node_id] = node
        self._nodes = configured
        self._observations: dict[str, NodeSnapshot] = {}
        self._snapshot_ttl = snapshot_ttl
        self._lock = RLock()

    @property
    def snapshot_ttl(self) -> timedelta:
        return self._snapshot_ttl

    def get_node(self, node_id: str) -> ComputeNode:
        try:
            return self._nodes[node_id]
        except KeyError:
            raise KeyError(f"compute node {node_id!r} is not configured") from None

    def configured_nodes(self) -> tuple[ComputeNode, ...]:
        return tuple(self._nodes[node_id] for node_id in sorted(self._nodes))

    def observe(self, snapshot: NodeSnapshot) -> NodeSnapshot:
        configured = self.get_node(snapshot.node.node_id)
        if snapshot.node.local != configured.local:
            raise ValueError("observed node locality conflicts with configured authority")
        if snapshot.node.origin != configured.origin:
            raise ValueError("observed node origin conflicts with configured origin")
        narrowed = replace(snapshot, node=configured)
        with self._lock:
            prior = self._observations.get(configured.node_id)
            if prior is not None and _utc(narrowed.observed_at, "observed_at") < _utc(
                prior.observed_at, "prior observed_at"
            ):
                raise ValueError("compute node observation time cannot move backwards")
            self._observations[configured.node_id] = narrowed
        return narrowed

    def last_observation(self, node_id: str) -> NodeSnapshot | None:
        self.get_node(node_id)
        with self._lock:
            return self._observations.get(node_id)

    def list_snapshots(self, *, now: datetime) -> tuple[NodeSnapshot, ...]:
        _utc(now, "now")
        with self._lock:
            return tuple(
                self._observations[node_id]
                for node_id in sorted(self._observations)
            )

    def is_stale(self, node_id: str, *, now: datetime) -> bool:
        current = _utc(now, "now")
        observed = self.last_observation(node_id)
        if observed is None:
            return True
        return current - _utc(observed.observed_at, "observed_at") > self._snapshot_ttl


__all__ = ["ComputeNodeRegistry"]
