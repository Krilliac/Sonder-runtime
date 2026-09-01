"""Bounded concurrent refresh of configured remote compute observations."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Callable

from .registry import ComputeNodeRegistry
from ...domain.compute_fabric import ComputeNode, NodeSnapshot


def refresh_remote_snapshots(
    registry: ComputeNodeRegistry,
    source,
    *,
    now: Callable[[], datetime],
    max_workers: int = 8,
) -> None:
    """Probe each configured remote once without serially multiplying timeouts."""
    if not isinstance(registry, ComputeNodeRegistry):
        raise TypeError("registry must be a ComputeNodeRegistry")
    if not callable(now):
        raise TypeError("now must be callable")
    if isinstance(max_workers, bool) or not isinstance(max_workers, int) or not 1 <= max_workers <= 32:
        raise ValueError("max_workers must be within 1..32")
    nodes = tuple(node for node in registry.configured_nodes() if not node.local)
    if not nodes:
        return

    def probe(node: ComputeNode) -> tuple[ComputeNode, datetime, NodeSnapshot | None, str | None]:
        received_at = now()
        try:
            snapshot = source.snapshot(node, now=received_at)
        except Exception as exc:
            return node, received_at, None, f"probe-failed:{type(exc).__name__}"
        return node, received_at, snapshot, None

    with ThreadPoolExecutor(
        max_workers=min(max_workers, len(nodes)),
        thread_name_prefix="sonder-compute-probe",
    ) as executor:
        futures = tuple(executor.submit(probe, node) for node in nodes)
        for future in as_completed(futures):
            node, received_at, snapshot, error_ref = future.result()
            if snapshot is None:
                registry.mark_probe_failed(
                    node.node_id,
                    received_at=received_at,
                    evidence_ref=error_ref or "probe-failed:unknown",
                )
            else:
                registry.observe(snapshot, received_at=received_at)


__all__ = ["refresh_remote_snapshots"]
