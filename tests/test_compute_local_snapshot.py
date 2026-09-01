from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from sonder_runtime.adapters.compute_fabric.local_snapshot import LocalComputeSnapshotSource
from sonder_runtime.domain.compute_fabric import (
    ComputeCapability,
    ComputeNode,
    NodeHealth,
    WorkloadKind,
)


NOW = datetime(2026, 8, 31, 20, tzinfo=timezone.utc)


def _local_node() -> ComputeNode:
    return ComputeNode(
        node_id="local",
        origin=None,
        local=True,
        allowed_workloads=frozenset({WorkloadKind.BUILD, WorkloadKind.TEST}),
        configured_capabilities=frozenset(ComputeCapability),
        workspace_mappings=frozenset({"sonder"}),
    )


def test_local_snapshot_reports_measured_values_without_inventing_cuda() -> None:
    environment = {
        "cpu_count": 16,
        "toolchains": {"cmake": "C:/cmake.exe", "docker": "C:/docker.exe"},
        "specialist_tools": {"sccache": "C:/sccache.exe"},
    }
    hardware = SimpleNamespace(
        system_ram_total_gb=32.0,
        system_ram_available_gb=12.0,
        system_ram_availability_live=True,
        gpu_vendor="nvidia",
        cuda_available=False,
        vram_total_gb=16.0,
        vram_free_gb=8.0,
        vram_availability_live=True,
    )
    disk = SimpleNamespace(total=500 << 30, free=400 << 30)
    source = LocalComputeSnapshotSource(
        environment_probe=lambda: environment,
        hardware_probe=lambda: hardware,
        disk_probe=lambda _path: disk,
        active_jobs=lambda: 2,
        load_probe=lambda _cpu: 0.25,
    )

    result = source.snapshot(_local_node(), now=NOW)

    assert result.health is NodeHealth.HEALTHY
    assert result.resources.cpu_count == 16
    assert result.resources.free_ram_bytes == 12 << 30
    assert result.resources.free_disk_bytes == 400 << 30
    assert result.resources.free_vram_bytes == 8 << 30
    assert result.active_jobs == 2
    assert ComputeCapability.CMAKE in result.live_capabilities
    assert ComputeCapability.DOCKER in result.live_capabilities
    assert ComputeCapability.SCCACHE in result.live_capabilities
    assert ComputeCapability.CUDA not in result.live_capabilities


def test_local_snapshot_keeps_unknown_free_capacity_unknown() -> None:
    hardware = SimpleNamespace(
        system_ram_total_gb=32.0,
        system_ram_available_gb=0.0,
        system_ram_availability_live=False,
        gpu_vendor="nvidia",
        cuda_available=True,
        vram_total_gb=16.0,
        vram_free_gb=0.0,
        vram_availability_live=False,
    )
    source = LocalComputeSnapshotSource(
        environment_probe=lambda: {"cpu_count": 8, "toolchains": {}, "specialist_tools": {}},
        hardware_probe=lambda: hardware,
        disk_probe=lambda _path: (_ for _ in ()).throw(OSError("unavailable")),
        active_jobs=lambda: 0,
        load_probe=lambda _cpu: None,
    )
    result = source.snapshot(_local_node(), now=NOW)
    assert result.resources.free_ram_bytes is None
    assert result.resources.free_disk_bytes is None
    assert result.resources.free_vram_bytes is None
    assert ComputeCapability.CUDA in result.live_capabilities


def test_local_snapshot_rejects_remote_identity_before_probing() -> None:
    source = LocalComputeSnapshotSource(
        environment_probe=lambda: pytest.fail("must not probe"),
    )
    remote = ComputeNode(
        node_id="remote",
        origin="https://remote:8443",
        local=False,
        allowed_workloads=frozenset({WorkloadKind.BUILD}),
    )
    with pytest.raises(ValueError, match="local node"):
        source.snapshot(remote, now=NOW)
