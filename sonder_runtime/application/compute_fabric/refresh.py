"""Bounded concurrent refresh of configured remote compute observations."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from datetime import datetime, timedelta
from typing import Callable

from .registry import ComputeNodeRegistry
from ...domain.compute_fabric import ComputeNode, NodeSnapshot, NodeHealth


def refresh_remote_snapshots(
    registry: ComputeNodeRegistry,
    source,
    *,
    now: Callable[[], datetime],
    max_workers: int = 8,
    refresh_after: timedelta | None = None,
) -> None:
    """Refresh due remotes with bounded running and queued probe work.

    Omitting refresh_after retains the explicit full-refresh operation. A
    configured interval never extends the registry's stale-evidence deadline.
    """
    if not isinstance(registry, ComputeNodeRegistry):
        raise TypeError("registry must be a ComputeNodeRegistry")
    if not callable(now):
        raise TypeError("now must be callable")
    if isinstance(max_workers, bool) or not isinstance(max_workers, int) or not 1 <= max_workers <= 32:
        raise ValueError("max_workers must be within 1..32")
    if refresh_after is not None and (
        not isinstance(refresh_after, timedelta) or not timedelta(0) < refresh_after <= registry.snapshot_ttl
    ):
        raise ValueError("refresh_after must be positive and no greater than snapshot TTL")
    nodes = tuple(node for node in registry.configured_nodes() if not node.local)
    if refresh_after is not None:
        current = now()
        def due(node):
            snapshot = registry.last_observation(node.node_id)
            return (snapshot is None or snapshot.health is not NodeHealth.HEALTHY
                    or not timedelta(0) <= current - snapshot.freshness_at < refresh_after)
        nodes = tuple(node for node in nodes if due(node))
    if not nodes:
        return

    def probe(node: ComputeNode) -> tuple[ComputeNode, datetime, NodeSnapshot | None, str | None]:
        request_started_at = now()
        try:
            snapshot = source.snapshot(node, now=request_started_at)
        except Exception as exc:
            received_at = now()
            return node, received_at, None, f"probe-failed:{type(exc).__name__}"
        received_at = now()
        return node, received_at, snapshot, None

    with ThreadPoolExecutor(
        max_workers=min(max_workers, len(nodes)),
        thread_name_prefix="sonder-compute-probe",
    ) as executor:
        remaining = iter(nodes)
        pending = {executor.submit(probe, node) for node in
                   (next(remaining) for _ in range(min(max_workers, len(nodes))))}
        while pending:
            finished, pending = wait(pending, return_when=FIRST_COMPLETED)
            for future in finished:
                node, received_at, snapshot, error_ref = future.result()
                if snapshot is None:
                    registry.mark_probe_failed(
                        node.node_id, received_at=received_at,
                        evidence_ref=error_ref or "probe-failed:unknown",
                    )
                else:
                    registry.observe(snapshot, received_at=received_at)
                next_node = next(remaining, None)
                if next_node is not None:
                    pending.add(executor.submit(probe, next_node))


__all__ = ["refresh_remote_snapshots"]
