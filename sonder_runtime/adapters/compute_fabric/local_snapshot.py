"""Conservative read-only observation of the local compute node."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
import importlib.util
import os
from pathlib import Path
import shutil
import threading
from typing import TYPE_CHECKING, cast

from ...domain.compute_fabric import (
    ComputeCapability,
    ComputeNode,
    NodeHealth,
    NodeResources,
    NodeSnapshot,
)
from ...platform import environment_probe, system_profile

if TYPE_CHECKING:
    from typing import Protocol

    class _HasLoadAvg(Protocol):
        def getloadavg(self) -> tuple[float, float, float]: ...

    class _DiskUsage(Protocol):
        @property
        def total(self) -> int: ...

        @property
        def free(self) -> int: ...


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
    "psql": ComputeCapability.DATABASE,
    "sqlite3": ComputeCapability.DATABASE,
    "mysql": ComputeCapability.DATABASE,
    "mariadb": ComputeCapability.DATABASE,
}

# Python-module capabilities: every module in one group must be importable
# (checked by spec lookup only, never imported); any satisfied group suffices.
_MODULE_CAPABILITIES: dict[ComputeCapability, tuple[tuple[str, ...], ...]] = {
    ComputeCapability.QLORA: (("peft", "bitsandbytes"),),
    ComputeCapability.EMBEDDINGS: (("sentence_transformers",), ("fastembed",)),
}


def _module_present(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError, AttributeError):
        return False


def _bytes_from_gib(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return None
    return int(round(float(value) * (1 << 30)))


def _default_load(cpu_count: int | None) -> float | None:
    if not cpu_count:
        return None
    try:
        one_minute = cast("_HasLoadAvg", os).getloadavg()[0]
    except (AttributeError, OSError):
        return None
    return max(0.0, min(1.0, one_minute / cpu_count))


class LocalComputeSnapshotSource:
    """Combine existing bounded host probes without inventing readiness."""

    def __init__(
        self,
        *,
        environment_probe: Callable[[], Mapping[str, object]] = environment_probe.probe,
        hardware_probe: Callable[[], object] = system_profile.detect_hardware,
        disk_probe: Callable[[Path], _DiskUsage] = shutil.disk_usage,
        active_jobs: Callable[[], int] = lambda: 0,
        load_probe: Callable[[int | None], float | None] = _default_load,
        tool_probe: Callable[[str], str | None] = shutil.which,
        module_probe: Callable[[str], bool] = _module_present,
        storage_path: str | os.PathLike[str] | None = None,
    ) -> None:
        self._environment_probe = environment_probe
        self._hardware_probe = hardware_probe
        self._disk_probe = disk_probe
        self._active_jobs = active_jobs
        self._load_probe = load_probe
        self._tool_probe = tool_probe
        self._module_probe = module_probe
        self._storage_path = Path.cwd() if storage_path is None else Path(storage_path)
        self._probed: frozenset[ComputeCapability] | None = None
        self._probed_lock = threading.Lock()

    def _probed_capabilities(self) -> frozenset[ComputeCapability]:
        """PATH/module presence for mapped tools, computed once per source.

        The shared environment profile lists only a fixed subset of tools.
        Looking up the rest costs filesystem stats per PATH entry, so the
        result is memoised like environment_probe.probe() (process-lifetime
        cache). No process is started and no network is used.
        """
        cached = self._probed
        if cached is not None:
            return cached
        with self._probed_lock:
            if self._probed is not None:
                return self._probed
            found: set[ComputeCapability] = set()
            for name, capability in _TOOL_CAPABILITIES.items():
                if capability in found:
                    continue
                try:
                    if self._tool_probe(name):
                        found.add(capability)
                except Exception:
                    continue
            for capability, groups in _MODULE_CAPABILITIES.items():
                try:
                    if any(all(self._module_probe(m) for m in group) for group in groups):
                        found.add(capability)
                except Exception:
                    continue
            self._probed = frozenset(found)
            return self._probed

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

        tools: dict[object, object] = {}
        for section in ("toolchains", "specialist_tools"):
            values = environment.get(section, {})
            if isinstance(values, Mapping):
                tools.update(values)
        for name in tools:
            capability = _TOOL_CAPABILITIES.get(str(name).casefold())
            if capability is not None:
                capabilities.add(capability)

        capabilities |= self._probed_capabilities()

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
