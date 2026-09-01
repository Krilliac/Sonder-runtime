from __future__ import annotations

from datetime import datetime, timezone
import json

import pytest

from sonder_runtime.adapters.compute_fabric.http_client import HttpsComputeSnapshotSource
from sonder_runtime.application.compute_fabric.wire import snapshot_to_wire
from sonder_runtime.domain.common.errors import DependencyUnavailable
from sonder_runtime.domain.compute_fabric import (
    ComputeCapability,
    ComputeNode,
    NodeHealth,
    NodeResources,
    NodeSnapshot,
    WorkloadKind,
)


NOW = datetime(2026, 8, 31, 20, tzinfo=timezone.utc)


class _Response:
    def __init__(self, status: int, body: bytes):
        self.status = status
        self._body = body

    def read(self, limit: int) -> bytes:
        return self._body[:limit]

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def _remote(node_id: str = "linux-node") -> ComputeNode:
    return ComputeNode(
        node_id=node_id,
        origin="https://linux-node.example:8443",
        local=False,
        allowed_workloads=frozenset({WorkloadKind.BUILD}),
        configured_capabilities=frozenset({ComputeCapability.CPU, ComputeCapability.CMAKE}),
        workspace_mappings=frozenset({"sonder"}),
    )


def _wire(node_id: str = "linux-node") -> bytes:
    worker_local = ComputeNode(
        node_id=node_id,
        origin=None,
        local=True,
        allowed_workloads=frozenset({WorkloadKind.BUILD}),
        configured_capabilities=frozenset({ComputeCapability.CPU, ComputeCapability.CMAKE}),
        workspace_mappings=frozenset({"sonder"}),
    )
    snapshot = NodeSnapshot(
        node=worker_local,
        observed_at=NOW,
        health=NodeHealth.HEALTHY,
        live_capabilities=frozenset({ComputeCapability.CPU, ComputeCapability.CMAKE}),
        advertised_workloads=frozenset({WorkloadKind.BUILD}),
        resources=NodeResources(cpu_count=16, free_ram_bytes=8 << 30),
        active_jobs=1,
    )
    return json.dumps({"object": "compute_snapshot", "snapshot": snapshot_to_wire(snapshot)}).encode()


def test_snapshot_client_reconstructs_configured_remote_authority() -> None:
    captured = {}

    def open_request(request, *, timeout):
        captured["url"] = request.full_url
        captured["authorization"] = request.headers["Authorization"]
        captured["timeout"] = timeout
        return _Response(200, _wire())

    source = HttpsComputeSnapshotSource(api_key="secret", opener=open_request)
    result = source.snapshot(_remote(), now=NOW)

    assert result.node is not None
    assert result.node.local is False
    assert result.node.origin == "https://linux-node.example:8443"
    assert result.node.node_id == "linux-node"
    assert result.resources.free_ram_bytes == 8 << 30
    assert captured == {
        "url": "https://linux-node.example:8443/v1/compute/snapshot",
        "authorization": "Bearer secret",
        "timeout": 2.0,
    }


@pytest.mark.parametrize(
    ("status", "body", "message"),
    (
        (302, b"", "redirect"),
        (200, _wire("different"), "identity"),
        (200, b"{not-json", "JSON"),
        (200, b"x" * 4097, "size"),
    ),
)
def test_snapshot_client_rejects_redirect_mismatch_invalid_json_and_oversize(
    status: int,
    body: bytes,
    message: str,
) -> None:
    source = HttpsComputeSnapshotSource(
        api_key="secret",
        max_response_bytes=4096,
        opener=lambda *_args, **_kwargs: _Response(status, body),
    )
    with pytest.raises(DependencyUnavailable, match=message):
        source.snapshot(_remote(), now=NOW)


def test_snapshot_client_rejects_empty_api_key_before_network() -> None:
    with pytest.raises(ValueError, match="API key"):
        HttpsComputeSnapshotSource(api_key="")
