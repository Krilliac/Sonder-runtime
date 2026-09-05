"""Indexed selection must match the existing scheduler, not approximate it."""
from dataclasses import replace
from datetime import timedelta
import pytest

from tests.test_compute_fabric_domain import _snapshot, NOW
from tests.test_compute_placement_service import _Transport, _LocalWorker
from sonder_runtime.application.compute_fabric.registry import ComputeNodeRegistry
from sonder_runtime.application.compute_fabric.service import ComputeFabricService
from sonder_runtime.domain.compute_fabric import (
    ComputeCapability as C, ComputePlacementScheduler, NodeHealth, NodeSnapshot,
    PlacementPolicy, WorkloadKind, WorkloadRequest,
)


def inventory(size, *, homogeneous=False):
    snapshots = tuple(_snapshot(f"node-{i:03}", local=i == 0,
        capabilities=(C.CPU, C.CMAKE) if homogeneous or i < 2 else (C.CPU,),
        free_ram=(size - i) << 30) for i in range(size))
    registry = ComputeNodeRegistry(tuple(item.node for item in snapshots))
    for item in snapshots:
        registry.observe(item)
    return registry, snapshots


def request(**changes):
    return replace(WorkloadRequest(request_id="indexed", kind=WorkloadKind.BUILD,
        workspace_mapping="sonder", allow_remote=True,
        required_capabilities=frozenset({C.CPU, C.CMAKE}),
        placement_policy=PlacementPolicy.RANK_ALL), **changes)


@pytest.mark.parametrize("size", [16, 64, 256])
@pytest.mark.parametrize("homogeneous", [False, True])
def test_exact_index_matches_linear_ranking_without_full_list(size, homogeneous, monkeypatch):
    registry, snapshots = inventory(size, homogeneous=homogeneous)
    query = request()
    scheduler = ComputePlacementScheduler()
    expected = scheduler.place(query, snapshots, now=NOW)
    service = ComputeFabricService(registry=registry, scheduler=scheduler, transport=_Transport(),
        local_worker=_LocalWorker(), now=lambda: NOW)
    monkeypatch.setattr(registry, "list_snapshots", lambda **kwargs: pytest.fail("full inventory copied"))
    monkeypatch.setattr(registry, "configured_nodes", lambda: pytest.fail("full configured list copied"))
    monkeypatch.setattr(NodeSnapshot, "digest", lambda self: pytest.fail("unchanged snapshot rehashed"))
    actual = service._place(query)
    assert actual.selected_node_id == expected.selected_node_id
    assert actual.ranked_node_ids == expected.ranked_node_ids
    assert len(actual.candidates) == (size if homogeneous else 2)
    assert actual.inventory_scope.candidate_scope == "indexed_structural_candidates"
    assert actual.inventory_scope.configured_count == size
    assert actual.inventory_scope.considered_count == len(actual.candidates)


def test_observation_changes_and_failed_probe_update_index_atomically():
    registry, snapshots = inventory(16)
    assert len(registry.candidates(request(), now=NOW).snapshots) == 2
    gained = replace(snapshots[3], live_capabilities=frozenset({C.CPU, C.CMAKE}))
    registry.observe(gained, received_at=NOW)
    # Live advertisements cannot widen configured capability authority.
    assert len(registry.candidates(request(), now=NOW).snapshots) == 2
    registry.observe(replace(snapshots[1], live_capabilities=frozenset({C.CPU})), received_at=NOW)
    assert len(registry.candidates(request(), now=NOW).snapshots) == 1
    registry.observe(snapshots[1], received_at=NOW)
    registry.mark_probe_failed("node-001", received_at=NOW, evidence_ref="probe-failed:TimeoutError")
    window = registry.candidates(request(), now=NOW)
    assert ComputePlacementScheduler().place(request(), window.snapshots, now=NOW).ranked_node_ids == ("node-000",)


def test_metric_expiry_has_no_write_dependency_and_heap_is_bounded():
    registry, snapshots = inventory(16)
    for _ in range(200):
        registry.observe(snapshots[0], received_at=NOW)
    assert registry.index_state_size()["expiry_entries"] <= 32
    assert registry.inventory_summary(now=NOW)["live"] == 16
    expired = registry.inventory_summary(now=NOW + timedelta(seconds=31))
    assert expired["live"] == 0 and expired["stale"] == 16
    assert registry.inventory_summary(now=NOW)["live"] == 16  # Clock rollback recomputes.


@pytest.mark.parametrize("changes", [
    {"required_capabilities": frozenset({C.CUDA})},
    {"any_capabilities": frozenset({C.CMAKE, C.CLANG})},
    {"any_capability_groups": (frozenset({C.CMAKE, C.CLANG}), frozenset({C.CUDA}))},
    {"required_model": "missing"}, {"workspace_mapping": "missing"},
    {"avoided_node_ids": frozenset({"node-000"})},
    {"preferred_node_ids": frozenset({"node-001"})},
    {"min_free_ram_bytes": 1000 << 30},
])
def test_index_is_exact_superset_for_every_structural_constraint(changes):
    registry, snapshots = inventory(64)
    query = request(**changes)
    expected = ComputePlacementScheduler().place(query, snapshots, now=NOW)
    selected = registry.candidates(query, now=NOW)
    actual = ComputePlacementScheduler().place(query, selected.snapshots, now=NOW)
    assert actual.ranked_node_ids == expected.ranked_node_ids


def test_index_detaches_mutable_observation_collections():
    registry, snapshots = inventory(16)
    models = ["stable"]
    registry.observe(replace(snapshots[0], models=models), received_at=NOW)
    models.append("injected")
    assert not registry.candidates(request(required_model="injected"), now=NOW).snapshots
    assert registry.last_observation("node-000").models == ("stable",)


def test_native_submit_uses_real_composed_index_and_durable_placement(tmp_path, monkeypatch, unattended_effects_allowed):
    import io, json
    from sonder_runtime.bootstrap.app import build_application
    from sonder_runtime.bootstrap.native_mcp import run_native_mcp
    from sonder_runtime.platform.config import SonderConfig, ComputeConfig, ComputeNodeConfig, Secrets, StateConfig
    from sonder_runtime.adapters.compute_fabric.local_snapshot import LocalComputeSnapshotSource
    from sonder_runtime.adapters.compute_fabric.http_client import HttpsComputeSnapshotSource, HttpsComputeJobTransport
    from sonder_runtime.application.compute_fabric.jobs import RemoteJobReceipt
    root = tmp_path / "sonder"
    root.mkdir()
    config = SonderConfig(state=StateConfig(home=str(tmp_path / "home"), workspace_roots=(str(root),)),
        secrets=Secrets(api_key="a" * 32), compute=ComputeConfig(allow_remote=True, nodes=tuple(
            ComputeNodeConfig(node_id=f"node-{i:03}", origin=f"https://node-{i:03}.example:8443",
                workloads=("build",), capabilities=("cpu", "cmake") if i < 2 else ("cpu",),
                workspace_mappings=("sonder",)) for i in range(15))))
    def snapshot(self, node, *, now):
        return NodeSnapshot(node=node, observed_at=now, health=NodeHealth.HEALTHY,
            live_capabilities=frozenset({C.CPU, C.CMAKE}), advertised_workloads=frozenset({WorkloadKind.BUILD}))
    monkeypatch.setattr(LocalComputeSnapshotSource, "snapshot", snapshot)
    monkeypatch.setattr(HttpsComputeSnapshotSource, "snapshot", snapshot)
    dispatched = []
    def submit(self, node, envelope):
        dispatched.append(node.node_id)
        return RemoteJobReceipt(worker_id=node.node_id, remote_job_id="remote-indexed",
            controller_job_id=envelope.controller_job_id, idempotency_key=envelope.idempotency_key,
            request_sha256=envelope.request_sha256, state="running")
    monkeypatch.setattr(HttpsComputeJobTransport, "submit", submit)
    app = build_application(config=config)
    monkeypatch.setattr(app.compute_registry(), "list_snapshots", lambda **kwargs: pytest.fail("composed service used full inventory"))
    messages = [dict(jsonrpc="2.0", id=1, method="initialize", params={"protocolVersion": "2.0", "capabilities": {"tools": {}}}),
        dict(jsonrpc="2.0", id=2, method="tools/call", params={"name": "compute_submit", "arguments": {
            "request_id": "indexed-native", "workload": "build", "catalog_entry_id": "cmake-build",
            "workspace_mapping": "sonder", "allow_remote": True, "idempotent": True}})]
    output = io.StringIO()
    try:
        run_native_mcp(app, input_stream=io.StringIO("\n".join(json.dumps(row) for row in messages) + "\n"), output_stream=output)
        assert dispatched == ["node-000"], output.getvalue()
        placements = list(app.job_registry().iter_kind("compute-placement", include_terminal=True))
        assert len(placements) == 1
        payload = app.job_registry().view(placements[0].identity.job_id).metadata["placement_payload"]
        assert payload["inventory_scope"]["considered_count"] == 2
        assert payload["inventory_scope"]["configured_count"] == 15
    finally:
        app.close_providers()
