from __future__ import annotations

from datetime import datetime, timezone
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import pytest

import sonder_runtime.adapters.web.lifecycle as sonder_lifecycle
import sonder_runtime.interfaces.http.serve as sonder_serve
from sonder_runtime.domain.compute_fabric import (
    ComputeCapability,
    ComputeNode,
    NodeHealth,
    NodeResources,
    NodeSnapshot,
    WorkloadKind,
)


pytestmark = pytest.mark.integration


@pytest.fixture()
def http_server(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("SONDER_HOME", str(home))
    monkeypatch.setenv("SONDER_OPERATIONS_DB", str(home / "operations.db"))
    sonder_lifecycle.reset_for_tests()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), sonder_serve.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()
    sonder_lifecycle.reset_for_tests()


def _get(base: str, path: str):
    try:
        with urllib.request.urlopen(base + path, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def _snapshot() -> NodeSnapshot:
    node = ComputeNode(
        node_id="worker-1",
        origin=None,
        local=True,
        allowed_workloads=frozenset({WorkloadKind.BUILD}),
        configured_capabilities=frozenset({ComputeCapability.CPU}),
    )
    return NodeSnapshot(
        node=node,
        observed_at=datetime(2026, 8, 31, 20, tzinfo=timezone.utc),
        health=NodeHealth.HEALTHY,
        live_capabilities=frozenset({ComputeCapability.CPU}),
        advertised_workloads=frozenset({WorkloadKind.BUILD}),
        resources=NodeResources(cpu_count=8),
    )


def test_snapshot_endpoint_requires_auth_before_probing(http_server, monkeypatch) -> None:
    monkeypatch.setattr(
        sonder_serve.Handler,
        "_request_auth_context",
        lambda _self: {"authorized": False, "mode": "api-key", "account": None},
    )
    monkeypatch.setattr(
        "sonder_runtime.bootstrap.app.default_app",
        lambda: pytest.fail("unauthenticated request must not probe"),
    )
    status, _body = _get(http_server, "/v1/compute/snapshot")
    assert status == 401


def test_snapshot_endpoint_returns_bounded_wire_projection(http_server, monkeypatch) -> None:
    monkeypatch.setattr(
        sonder_serve.Handler,
        "_request_auth_context",
        lambda _self: {"authorized": True, "mode": "api-key", "account": None},
    )
    monkeypatch.setattr(
        "sonder_runtime.bootstrap.app.default_app",
        lambda: SimpleNamespace(compute_snapshot=lambda: _snapshot()),
    )
    status, body = _get(http_server, "/v1/compute/snapshot")
    assert status == 200
    assert body["object"] == "compute_snapshot"
    assert body["snapshot"]["node_id"] == "worker-1"
    assert "origin" not in body["snapshot"]
    assert "workspace_mappings" not in body["snapshot"]
