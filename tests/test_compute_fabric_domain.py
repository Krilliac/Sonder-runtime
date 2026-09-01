from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from sonder_runtime.domain.compute_fabric import (
    CandidateDecision,
    ComputeCapability,
    ComputeNode,
    ComputePlacementScheduler,
    NodeHealth,
    NodeResources,
    NodeSnapshot,
    WorkloadKind,
    WorkloadRequest,
)


NOW = datetime(2026, 8, 31, 20, tzinfo=timezone.utc)


def _snapshot(
    node_id: str,
    *,
    local: bool = False,
    free_ram: int | None = 16 << 30,
    free_disk: int | None = 100 << 30,
    free_vram: int | None = None,
    load: float | None = 0.2,
    jobs: int = 0,
    health: NodeHealth = NodeHealth.HEALTHY,
    capabilities: tuple[ComputeCapability, ...] = (
        ComputeCapability.CPU,
        ComputeCapability.CMAKE,
    ),
    allowed: tuple[WorkloadKind, ...] = (
        WorkloadKind.BUILD,
        WorkloadKind.TEST,
    ),
    workspaces: tuple[str, ...] = ("sonder",),
) -> NodeSnapshot:
    node = ComputeNode(
        node_id=node_id,
        origin=None if local else f"https://{node_id}.example:8443",
        local=local,
        allowed_workloads=frozenset(allowed),
        configured_capabilities=frozenset(capabilities),
        workspace_mappings=frozenset(workspaces),
    )
    return NodeSnapshot(
        node=node,
        observed_at=NOW,
        health=health,
        live_capabilities=frozenset(capabilities),
        advertised_workloads=frozenset(allowed),
        resources=NodeResources(
            free_ram_bytes=free_ram,
            free_disk_bytes=free_disk,
            free_vram_bytes=free_vram,
            load_fraction=load,
        ),
        active_jobs=jobs,
        round_trip_ms=5.0,
    )


def test_remote_nodes_require_https_without_credentials() -> None:
    with pytest.raises(ValueError, match="HTTPS"):
        ComputeNode(
            node_id="bad",
            origin="http://worker.example:8443",
            local=False,
            allowed_workloads=frozenset({WorkloadKind.BUILD}),
        )
    with pytest.raises(ValueError, match="credentials"):
        ComputeNode(
            node_id="bad",
            origin="https://user:secret@worker.example:8443",
            local=False,
            allowed_workloads=frozenset({WorkloadKind.BUILD}),
        )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("free_ram_bytes", -1),
        ("free_disk_bytes", -1),
        ("free_vram_bytes", -1),
        ("load_fraction", -0.1),
        ("load_fraction", 1.1),
        ("load_fraction", float("nan")),
    ),
)
def test_resources_reject_negative_or_non_finite_values(field: str, value: object) -> None:
    with pytest.raises(ValueError):
        NodeResources(**{field: value})


def test_effective_authority_is_intersection_of_configuration_and_telemetry() -> None:
    configured = _snapshot(
        "node",
        capabilities=(ComputeCapability.CPU, ComputeCapability.CMAKE),
        allowed=(WorkloadKind.BUILD,),
    )
    observed = replace(
        configured,
        live_capabilities=frozenset(
            {ComputeCapability.CPU, ComputeCapability.CMAKE, ComputeCapability.DOCKER}
        ),
        advertised_workloads=frozenset({WorkloadKind.BUILD, WorkloadKind.SERVICE}),
    )
    assert observed.effective_capabilities == frozenset(
        {ComputeCapability.CPU, ComputeCapability.CMAKE}
    )
    assert observed.effective_workloads == frozenset({WorkloadKind.BUILD})


def test_scheduler_rejects_stale_or_under_resourced_nodes_and_is_deterministic() -> None:
    request = WorkloadRequest(
        request_id="request-1",
        kind=WorkloadKind.BUILD,
        required_capabilities=frozenset({ComputeCapability.CMAKE}),
        workspace_mapping="sonder",
        min_free_ram_bytes=8 << 30,
        allow_remote=True,
    )
    stale = replace(_snapshot("stale"), observed_at=NOW - timedelta(minutes=2))
    low = _snapshot("low", free_ram=4 << 30)
    best = _snapshot("node-b", load=0.1)
    lexical_tie = _snapshot("node-a", load=0.1)

    result = ComputePlacementScheduler(snapshot_ttl=timedelta(seconds=30)).place(
        request,
        (stale, low, best, lexical_tie),
        now=NOW,
    )

    assert result.selected_node_id == "node-a"
    assert {item.node_id: item.reason_code for item in result.candidates} == {
        "low": "insufficient_ram",
        "node-a": "eligible",
        "node-b": "eligible",
        "stale": "stale",
    }


def test_scheduler_reports_each_hard_constraint_without_fallback_guessing() -> None:
    cases = (
        (_snapshot("unhealthy", health=NodeHealth.UNHEALTHY), "unhealthy"),
        (_snapshot("wrong-kind", allowed=(WorkloadKind.TEST,)), "workload_not_allowed"),
        (_snapshot("missing-cap", capabilities=(ComputeCapability.CPU,)), "missing_capability"),
        (_snapshot("missing-workspace", workspaces=()), "workspace_unavailable"),
        (_snapshot("unknown-ram", free_ram=None), "ram_unknown"),
        (_snapshot("disk", free_disk=1), "insufficient_disk"),
        (_snapshot("load", load=0.95), "load_too_high"),
        (_snapshot("avoided"), "node_avoided"),
    )
    scheduler = ComputePlacementScheduler()
    for node, expected in cases:
        request = WorkloadRequest(
            request_id=f"request-{node.node.node_id}",
            kind=WorkloadKind.BUILD,
            required_capabilities=frozenset({ComputeCapability.CMAKE}),
            workspace_mapping="sonder",
            min_free_ram_bytes=1,
            min_free_disk_bytes=2,
            max_load_fraction=0.9,
            allow_remote=True,
            avoided_node_ids=frozenset({"avoided"}),
        )
        decision = scheduler.place(request, (node,), now=NOW)
        assert decision.selected_node_id is None
        assert decision.candidates == (
            CandidateDecision(node.node.node_id, False, expected),
        )


def test_private_work_never_uses_remote_node() -> None:
    request = WorkloadRequest(
        request_id="private",
        kind=WorkloadKind.TEST,
        local_only=True,
    )
    decision = ComputePlacementScheduler().place(request, (_snapshot("remote"),), now=NOW)
    assert decision.selected_node_id is None
    assert decision.candidates[0].reason_code == "local_only"


def test_inference_placement_remains_owned_by_model_gateway() -> None:
    request = WorkloadRequest(request_id="inference", kind=WorkloadKind.INFERENCE)
    with pytest.raises(ValueError, match="model gateway"):
        ComputePlacementScheduler().place(request, (_snapshot("remote"),), now=NOW)


def test_remote_compute_requires_per_workload_opt_in() -> None:
    scheduler = ComputePlacementScheduler()
    snapshot = _snapshot("linux-node")

    denied = scheduler.place(
        WorkloadRequest("without-consent", WorkloadKind.BUILD),
        (snapshot,),
        now=NOW,
    )
    assert denied.selected_node_id is None
    assert denied.candidates[0].reason_code == "remote_not_allowed"

    allowed = scheduler.place(
        WorkloadRequest(
            "with-consent", WorkloadKind.BUILD, allow_remote=True
        ),
        (snapshot,),
        now=NOW,
    )
    assert allowed.selected_node_id == "linux-node"


def test_unknown_gpu_headroom_does_not_satisfy_a_gpu_floor() -> None:
    request = WorkloadRequest(
        request_id="gpu",
        kind=WorkloadKind.BUILD,
        min_free_vram_bytes=1,
        allow_remote=True,
    )
    decision = ComputePlacementScheduler().place(request, (_snapshot("node"),), now=NOW)
    assert decision.candidates[0].reason_code == "vram_unknown"


def test_placement_and_snapshot_digests_are_stable_and_content_bound() -> None:
    first = _snapshot("node")
    second = _snapshot("node")
    assert first.digest() == second.digest()
    assert first.digest() != replace(first, active_jobs=1).digest()

    request = WorkloadRequest("request", WorkloadKind.BUILD, allow_remote=True)
    decision = ComputePlacementScheduler().place(request, (first,), now=NOW)
    assert len(decision.request_digest) == 64
    assert decision.snapshot_digests == (("node", first.digest()),)
