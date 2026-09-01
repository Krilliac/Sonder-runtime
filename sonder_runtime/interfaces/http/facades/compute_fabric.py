"""Read-only HTTP projection for the local compute-node snapshot."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from ....application.compute_fabric.wire import snapshot_to_wire
from ....domain.compute_fabric import NodeSnapshot


@dataclass(frozen=True, slots=True)
class ComputeFabricHttpResult:
    body: dict[str, Any]
    status_code: int = 200


def dispatch_compute_snapshot(
    snapshot_factory: Callable[[], NodeSnapshot],
) -> ComputeFabricHttpResult:
    snapshot = snapshot_factory()
    if not isinstance(snapshot, NodeSnapshot):
        raise TypeError("compute snapshot factory returned an invalid value")
    return ComputeFabricHttpResult({
        "object": "compute_snapshot",
        "snapshot": snapshot_to_wire(snapshot),
    })


__all__ = ["ComputeFabricHttpResult", "dispatch_compute_snapshot"]
