"""Compute-fabric infrastructure adapters."""

from .local_snapshot import LocalComputeSnapshotSource
from .http_client import HttpsComputeSnapshotSource

__all__ = ["HttpsComputeSnapshotSource", "LocalComputeSnapshotSource"]
