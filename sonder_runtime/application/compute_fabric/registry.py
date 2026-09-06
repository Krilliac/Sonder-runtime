"""Configured compute-node authority and last-observation registry."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from threading import RLock
from bisect import bisect_right
import base64
import json

from ...domain.compute_fabric import ComputeNode, NodeHealth, NodeSnapshot
from .index import ComputeIndex


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
            if any(not isinstance(getattr(node, name), frozenset) for name in
                   ("allowed_workloads", "configured_capabilities", "workspace_mappings")):
                node = replace(node, allowed_workloads=frozenset(node.allowed_workloads),
                               configured_capabilities=frozenset(node.configured_capabilities),
                               workspace_mappings=frozenset(node.workspace_mappings))
            configured[node.node_id] = node
        self._nodes = configured
        self._observations: dict[str, NodeSnapshot] = {}
        self._probe_errors: dict[str, str] = {}
        self._snapshot_ttl = snapshot_ttl
        self._lock = RLock()
        self._index = ComputeIndex(tuple(configured.values()), snapshot_ttl)
        self._configured_sorted = tuple(configured[identity] for identity in self._index.ids)

    @property
    def snapshot_ttl(self) -> timedelta:
        return self._snapshot_ttl

    def get_node(self, node_id: str) -> ComputeNode:
        try:
            return self._nodes[node_id]
        except KeyError:
            raise KeyError(f"compute node {node_id!r} is not configured") from None

    def configured_nodes(self) -> tuple[ComputeNode, ...]:
        return self._configured_sorted

    def observe(
        self,
        snapshot: NodeSnapshot,
        *,
        received_at: datetime | None = None,
    ) -> NodeSnapshot:
        configured = self.get_node(snapshot.node.node_id)
        if snapshot.node.local != configured.local:
            raise ValueError("observed node locality conflicts with configured authority")
        if snapshot.node.origin != configured.origin:
            raise ValueError("observed node origin conflicts with configured origin")
        controller_time = _utc(
            received_at or snapshot.received_at or snapshot.observed_at,
            "received_at",
        )
        narrowed = replace(
            snapshot,
            node=configured,
            received_at=controller_time,
            live_capabilities=frozenset(snapshot.live_capabilities),
            advertised_workloads=frozenset(snapshot.advertised_workloads),
            models=tuple(snapshot.models),
        )
        with self._lock:
            prior = self._observations.get(configured.node_id)
            if prior is not None and controller_time < _utc(
                prior.received_at or prior.observed_at,
                "prior received_at",
            ):
                raise ValueError("compute node receipt time cannot move backwards")
            self._index.update(narrowed)
            self._observations[configured.node_id] = narrowed
            self._probe_errors.pop(configured.node_id, None)
        return narrowed

    def mark_probe_failed(
        self,
        node_id: str,
        *,
        received_at: datetime,
        evidence_ref: str,
    ) -> NodeSnapshot:
        configured = self.get_node(node_id)
        controller_time = _utc(received_at, "received_at")
        if not isinstance(evidence_ref, str) or not evidence_ref or len(evidence_ref) > 512:
            raise ValueError("probe failure evidence must be a bounded string")
        with self._lock:
            prior = self._observations.get(node_id)
            if prior is not None and controller_time < _utc(
                prior.received_at or prior.observed_at,
                "prior received_at",
            ):
                raise ValueError("compute node receipt time cannot move backwards")
            failed = (
                NodeSnapshot(
                    node=configured,
                    observed_at=controller_time,
                    received_at=controller_time,
                    health=NodeHealth.UNHEALTHY,
                    evidence_ref=evidence_ref,
                )
                if prior is None
                else replace(
                    prior,
                    node=configured,
                    received_at=controller_time,
                    health=NodeHealth.UNHEALTHY,
                )
            )
            self._index.update(failed)
            self._observations[node_id] = failed
            self._probe_errors[node_id] = evidence_ref
            return failed

    def last_probe_error(self, node_id: str) -> str | None:
        self.get_node(node_id)
        with self._lock:
            return self._probe_errors.get(node_id)

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
        return current - observed.freshness_at > self._snapshot_ttl

    def candidates(self, request, *, now: datetime, local=None):
        _utc(now, "now")
        with self._lock:
            return self._index.candidates(request, local=local)

    def capability_candidates(
        self,
        *,
        required_capabilities=(),
        any_capabilities=(),
        any_capability_groups=(),
        now: datetime,
        local=None,
    ):
        """Return a bounded indexed capability view without copying inventory."""
        _utc(now, "now")
        with self._lock:
            ids = sorted(self._index.capability_ids(
                required_capabilities=required_capabilities,
                any_capabilities=any_capabilities,
                any_capability_groups=any_capability_groups,
                local=local,
            ))
            configured_count = len(self._nodes) if local is None else len(
                self._index.static.get(("local", local), ())
            )
            observed_count = len(self._observations) if local is None else len(
                self._index.live.get(("local", local), ())
            )
            return self._index.candidates_from_ids(
                ids,
                configured_count=configured_count,
                observed_count=observed_count,
            )

    def configured_candidates(self, request, *, local=None):
        with self._lock:
            return tuple(self._nodes[identity] for identity in sorted(
                self._index.matching(request, observed=False, local=local)))

    def inventory_summary(self, *, now: datetime):
        with self._lock:
            return self._index.summary(_utc(now, "now"))

    def index_state_size(self):
        with self._lock:
            return {"expiry_entries": len(self._index.expiry),
                    "observations": len(self._observations), "live_postings": len(self._index.live)}

    @property
    def observation_revision(self):
        with self._lock:
            return self._index.revision

    def observation_version(self, node_id):
        self.get_node(node_id)
        with self._lock:
            return self._observations.get(node_id), self._index.versions.get(node_id, 0)

    def inventory_page(self, *, now: datetime, limit=32, cursor=None):
        current = _utc(now, "now")
        if type(limit) is not int or not 1 <= limit <= 64:
            raise ValueError("inventory limit must be within 1..64")
        with self._lock:
            after = ""
            if cursor is not None:
                if not isinstance(cursor, str) or not 1 <= len(cursor) <= 1024:
                    raise ValueError("invalid inventory cursor")
                try:
                    value = json.loads(base64.b64decode(cursor.encode("ascii"), altchars=b"-_", validate=True))
                    if (not isinstance(value, list) or len(value) != 2 or value[0] != self._index.generation
                            or not isinstance(value[1], str) or value[1] not in self._nodes):
                        raise ValueError("invalid inventory cursor")
                    after = value[1]
                except (ValueError, UnicodeError, TypeError):
                    raise ValueError("invalid or expired inventory cursor") from None
            start = bisect_right(self._index.ids, after)
            identities = self._index.ids[start:start + limit + 1]
            has_more = len(identities) > limit
            selected = identities[:limit]
            rows = []
            for identity in selected:
                node = self._nodes[identity]
                observed = self._observations.get(identity)
                rows.append({"node_id": identity, "local": node.local, "configured": True,
                    "observed": observed is not None,
                    "stale": observed is None or current - observed.freshness_at > self._snapshot_ttl,
                    "health": observed.health.value if observed else "unknown",
                    "observation_revision": self._index.versions.get(identity, 0),
                    "observed_at": observed.observed_at.isoformat() if observed else None,
                    "received_at": observed.received_at.isoformat() if observed and observed.received_at else None,
                    "active_jobs": observed.active_jobs if observed else None,
                    "workloads": sorted(value.value for value in node.allowed_workloads),
                    "capabilities": sorted(value.value for value in observed.effective_capabilities) if observed else [],
                    "evidence_ref": observed.evidence_ref if observed else None,
                    "probe_error": self._probe_errors.get(identity, "")})
            next_cursor = (base64.urlsafe_b64encode(json.dumps([self._index.generation, selected[-1]],
                           separators=(",", ":")).encode()).decode() if has_more else None)
            return {"nodes": rows, "has_more": has_more, "next_cursor": next_cursor,
                    "registry_generation": self._index.generation,
                    "observation_revision": self._index.revision, "captured_at": current.isoformat(),
                    "configured_count": len(self._nodes), "observation_consistency": "live_per_page"}


__all__ = ["ComputeNodeRegistry"]
