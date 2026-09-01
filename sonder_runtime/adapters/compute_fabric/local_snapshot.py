"""Conservative read-only observation of the local compute node."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
import os
from pathlib import Path
import shutil
from typing import Any

from ...domain.compute_fabric import (
    ComputeCapability,
    ComputeNode,
    NodeHealth,
    NodeResources,
    NodeSnapshot,
)
from ...platform import environment_probe, system_profile


_TOOL_CAPABILITIES = {
    "cmake": ComputeCapability.CMAKE,
    "cl": ComputeCapability.MSVC,
    "msbuild": ComputeCapability.MSVC,
    "clang": ComputeCapability.CLANG,
    "clang++": ComputeCapability.CLANG,
    "clangd": ComputeCapability.CLANGD,
    "clang-tidy": ComputeCapability.CLANG_TIDY,
    "docker": ComputeCapability.DOCKER,
    "podman": ComputeCapability.PODMAN,
    "pytest": ComputeCapability.PYTEST,
    "ffmpeg": ComputeCapability.FFMPEG,
    "blender": ComputeCapability.BLENDER,
    "distcc": ComputeCapability.DISTCC,
    "sccache": ComputeCapability.SCCACHE,
    "ollama": ComputeCapability.OLLAMA,
    "llama-server": ComputeCapability.LLAMACPP,
}


def _bytes_from_gib(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return None
    return int(round(float(value) * (1 << 30)))


def _default_load(cpu_count: int | None) -> float | None:
    if not cpu_count:
        return None
    try:
        one_minute = os.getloadavg()[0]
    except (AttributeError, OSError):
        return None
    return max(0.0, min(1.0, one_minute / cpu_count))


class LocalComputeSnapshotSource:
    """Combine existing bounded host probes without inventing readiness."""

    def __init__(
        self,
        *,
        environment_probe: Callable[[], Mapping[str, Any]] = environment_probe.probe,
        hardware_probe: Callable[[], Any] = system_profile.detect_hardware,
        disk_probe: Callable[[Path], Any] = shutil.disk_usage,
        active_jobs: Callable[[], int] = lambda: 0,
        load_probe: Callable[[int | None], float | None] = _default_load,
        storage_path: Path | None = None,
    ) -> None:
        self._environment_probe = environment_probe
        self._hardware_probe = hardware_probe
        self._disk_probe = disk_probe
        self._active_jobs = active_jobs
        self._load_probe = load_probe
        self._storage_path = Path.cwd() if storage_path is None else Path(storage_path)

    def snapshot(self, node: ComputeNode, *, now: datetime) -> NodeSnapshot:
        if not isinstance(node, ComputeNode) or not node.local:
            raise ValueError("local snapshot source requires a configured local node")
        environment = self._environment_probe()
        hardware = self._hardware_probe()
        cpu_count = environment.get("cpu_count")
        if isinstance(cpu_count, bool) or not isinstance(cpu_count, int) or cpu_count < 1:
            cpu_count = None

        capabilities: set[ComputeCapability] = set()
        if cpu_count:
            capabilities.add(ComputeCapability.CPU)
        total_ram = _bytes_from_gib(getattr(hardware, "system_ram_total_gb", None))
        ram_live = bool(getattr(hardware, "system_ram_availability_live", False))
        free_ram = (
            _bytes_from_gib(getattr(hardware, "system_ram_available_gb", None))
            if ram_live else None
        )
        if total_ram:
            capabilities.add(ComputeCapability.RAM)

        tools: dict[str, Any] = {}
        for section in ("toolchains", "specialist_tools"):
            values = environment.get(section, {})
            if isinstance(values, Mapping):
                tools.update(values)
        for name in tools:
            capability = _TOOL_CAPABILITIES.get(str(name).casefold())
            if capability is not None:
                capabilities.add(capability)

        if bool(getattr(hardware, "cuda_available", False)):
            capabilities.add(ComputeCapability.CUDA)
        total_vram = _bytes_from_gib(getattr(hardware, "vram_total_gb", None))
        vram_live = bool(getattr(hardware, "vram_availability_live", False))
        free_vram = (
            _bytes_from_gib(getattr(hardware, "vram_free_gb", None))
            if vram_live else None
        )

        total_disk = free_disk = None
        try:
            disk = self._disk_probe(self._storage_path)
            total_disk = int(disk.total)
            free_disk = int(disk.free)
            if total_disk >= 0 and free_disk >= 0:
                capabilities.add(ComputeCapability.STORAGE)
            else:
                total_disk = free_disk = None
        except (OSError, ValueError, TypeError, AttributeError):
            pass

        try:
            job_count = self._active_jobs()
        except Exception:
            job_count = 0
        if isinstance(job_count, bool) or not isinstance(job_count, int) or job_count < 0:
            job_count = 0
        try:
            load = self._load_probe(cpu_count)
        except Exception:
            load = None

        return NodeSnapshot(
            node=node,
            observed_at=now,
            health=NodeHealth.HEALTHY if cpu_count else NodeHealth.UNKNOWN,
            live_capabilities=frozenset(capabilities),
            advertised_workloads=node.allowed_workloads,
            resources=NodeResources(
                cpu_count=cpu_count,
                total_ram_bytes=total_ram,
                free_ram_bytes=free_ram,
                total_disk_bytes=total_disk,
                free_disk_bytes=free_disk,
                total_vram_bytes=total_vram,
                free_vram_bytes=free_vram,
                load_fraction=load,
            ),
            active_jobs=job_count,
            round_trip_ms=0.0,
            evidence_ref="local-bounded-probes",
        )


__all__ = ["LocalComputeSnapshotSource"]
