"""Pure contracts and deterministic placement for Sonder compute nodes.

This module performs no probing, persistence, process creation, or network I/O.
Configuration establishes a node's maximum authority; a live snapshot can only
narrow that authority before the scheduler evaluates a workload.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import StrEnum
import hashlib
import json
import math
import re
from typing import Any, Iterable


_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_HTTPS_ORIGIN = re.compile(
    r"^https://(?:\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9._-]+):([0-9]{1,5})/?$"
)


class WorkloadKind(StrEnum):
    BUILD = "build"
    TEST = "test"
    INDEX = "index"
    ANALYSIS = "analysis"
    FUZZ = "fuzz"
    EMBEDDING = "embedding"
    TRAINING = "training"
    RENDER = "render"
    ENCODE = "encode"
    SERVICE = "service"
    CONTAINER = "container"
    STORAGE = "storage"
    INFERENCE = "inference"


class ComputeCapability(StrEnum):
    CPU = "cpu"
    RAM = "ram"
    CUDA = "cuda"
    OLLAMA = "ollama"
    LLAMACPP = "llamacpp"
    DOCKER = "docker"
    PODMAN = "podman"
    KVM = "kvm"
    CMAKE = "cmake"
    MSVC = "msvc"
    CLANG = "clang"
    CLANGD = "clangd"
    CLANG_TIDY = "clang-tidy"
    SCCACHE = "sccache"
    DISTCC = "distcc"
    PYTEST = "pytest"
    EMBEDDINGS = "embeddings"
    QLORA = "qlora"
    FFMPEG = "ffmpeg"
    BLENDER = "blender"
    STORAGE = "storage"
    DATABASE = "database"


class NodeHealth(StrEnum):
    UNKNOWN = "unknown"
    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"


def _require_identity(value: str, label: str) -> str:
    if not isinstance(value, str) or not _IDENTITY.fullmatch(value):
        raise ValueError(f"{label} must be a bounded stable identity")
    return value


def _optional_nonnegative_int(value: int | None, label: str) -> None:
    if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
        raise ValueError(f"{label} must be a non-negative integer or None")


def _optional_fraction(value: float | None, label: str) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a finite fraction or None")
    if not math.isfinite(float(value)) or not 0.0 <= float(value) <= 1.0:
        raise ValueError(f"{label} must be between 0 and 1")


def _aware_utc(value: datetime, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _canonical_digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class NodeResources:
    cpu_count: int | None = None
    total_ram_bytes: int | None = None
    free_ram_bytes: int | None = None
    total_disk_bytes: int | None = None
    free_disk_bytes: int | None = None
    total_vram_bytes: int | None = None
    free_vram_bytes: int | None = None
    load_fraction: float | None = None
    gpu_utilization_fraction: float | None = None

    def __post_init__(self) -> None:
        for name in (
            "cpu_count",
            "total_ram_bytes",
            "free_ram_bytes",
            "total_disk_bytes",
            "free_disk_bytes",
            "total_vram_bytes",
            "free_vram_bytes",
        ):
            _optional_nonnegative_int(getattr(self, name), name)
        if self.cpu_count == 0:
            raise ValueError("cpu_count must be positive when known")
        _optional_fraction(self.load_fraction, "load_fraction")
        _optional_fraction(self.gpu_utilization_fraction, "gpu_utilization_fraction")
        for free_name, total_name in (
            ("free_ram_bytes", "total_ram_bytes"),
            ("free_disk_bytes", "total_disk_bytes"),
            ("free_vram_bytes", "total_vram_bytes"),
        ):
            free = getattr(self, free_name)
            total = getattr(self, total_name)
            if free is not None and total is not None and free > total:
                raise ValueError(f"{free_name} cannot exceed {total_name}")

    def as_dict(self) -> dict[str, int | float | None]:
        return {
            "cpu_count": self.cpu_count,
            "total_ram_bytes": self.total_ram_bytes,
            "free_ram_bytes": self.free_ram_bytes,
            "total_disk_bytes": self.total_disk_bytes,
            "free_disk_bytes": self.free_disk_bytes,
            "total_vram_bytes": self.total_vram_bytes,
            "free_vram_bytes": self.free_vram_bytes,
            "load_fraction": self.load_fraction,
            "gpu_utilization_fraction": self.gpu_utilization_fraction,
        }


@dataclass(frozen=True, slots=True)
class ComputeNode:
    node_id: str
    origin: str | None
    local: bool
    allowed_workloads: frozenset[WorkloadKind]
    configured_capabilities: frozenset[ComputeCapability] = frozenset()
    workspace_mappings: frozenset[str] = frozenset()
    preference_weight: float = 0.0

    def __post_init__(self) -> None:
        _require_identity(self.node_id, "node_id")
        if not isinstance(self.local, bool):
            raise ValueError("local must be boolean")
        if not self.allowed_workloads:
            raise ValueError("allowed_workloads must not be empty")
        if any(not isinstance(item, WorkloadKind) for item in self.allowed_workloads):
            raise ValueError("allowed_workloads contains an unknown workload")
        if any(not isinstance(item, ComputeCapability) for item in self.configured_capabilities):
            raise ValueError("configured_capabilities contains an unknown capability")
        for mapping in self.workspace_mappings:
            _require_identity(mapping, "workspace mapping")
        if not isinstance(self.preference_weight, (int, float)) or isinstance(self.preference_weight, bool):
            raise ValueError("preference_weight must be finite")
        if not math.isfinite(float(self.preference_weight)) or not -100.0 <= float(self.preference_weight) <= 100.0:
            raise ValueError("preference_weight must be between -100 and 100")
        if self.local:
            if self.origin is not None:
                raise ValueError("local nodes must not define an origin")
            return
        if not isinstance(self.origin, str):
            raise ValueError("remote nodes require an HTTPS origin")
        match = _HTTPS_ORIGIN.fullmatch(self.origin)
        if match is None and "@" in self.origin:
            raise ValueError("remote node origins must not contain inline credentials")
        if match is None:
            raise ValueError("remote nodes require an HTTPS origin with an explicit port")
        if not 1 <= int(match.group(1)) <= 65_535:
            raise ValueError("remote nodes require a valid HTTPS port")

    def as_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "origin": self.origin,
            "local": self.local,
            "allowed_workloads": sorted(item.value for item in self.allowed_workloads),
            "configured_capabilities": sorted(
                item.value for item in self.configured_capabilities
            ),
            "workspace_mappings": sorted(self.workspace_mappings),
            "preference_weight": float(self.preference_weight),
        }


@dataclass(frozen=True, slots=True)
class NodeSnapshot:
    node: ComputeNode
    observed_at: datetime
    health: NodeHealth = NodeHealth.UNKNOWN
    live_capabilities: frozenset[ComputeCapability] = frozenset()
    advertised_workloads: frozenset[WorkloadKind] = frozenset()
    resources: NodeResources = field(default_factory=NodeResources)
    active_jobs: int = 0
    round_trip_ms: float | None = None
    models: tuple[str, ...] = ()
    evidence_ref: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.node, ComputeNode):
            raise ValueError("node must be a ComputeNode")
        _aware_utc(self.observed_at, "observed_at")
        if not isinstance(self.health, NodeHealth):
            raise ValueError("health must be a NodeHealth")
        if any(not isinstance(item, ComputeCapability) for item in self.live_capabilities):
            raise ValueError("live_capabilities contains an unknown capability")
        if any(not isinstance(item, WorkloadKind) for item in self.advertised_workloads):
            raise ValueError("advertised_workloads contains an unknown workload")
        _optional_nonnegative_int(self.active_jobs, "active_jobs")
        if self.round_trip_ms is not None:
            if (
                isinstance(self.round_trip_ms, bool)
                or not isinstance(self.round_trip_ms, (int, float))
                or not math.isfinite(float(self.round_trip_ms))
                or self.round_trip_ms < 0
                or self.round_trip_ms > 3_600_000
            ):
                raise ValueError("round_trip_ms must be finite and bounded")
        if len(self.models) > 256 or any(
            not isinstance(model, str) or not model or len(model) > 256
            for model in self.models
        ):
            raise ValueError("models must be a bounded tuple of identities")
        if self.evidence_ref is not None and (
            not isinstance(self.evidence_ref, str) or len(self.evidence_ref) > 512
        ):
            raise ValueError("evidence_ref must be bounded")

    @property
    def effective_capabilities(self) -> frozenset[ComputeCapability]:
        return self.node.configured_capabilities & self.live_capabilities

    @property
    def effective_workloads(self) -> frozenset[WorkloadKind]:
        return self.node.allowed_workloads & self.advertised_workloads

    def as_dict(self) -> dict[str, Any]:
        return {
            "node": self.node.as_dict(),
            "observed_at": _aware_utc(self.observed_at, "observed_at")
            .isoformat()
            .replace("+00:00", "Z"),
            "health": self.health.value,
            "live_capabilities": sorted(item.value for item in self.live_capabilities),
            "advertised_workloads": sorted(item.value for item in self.advertised_workloads),
            "resources": self.resources.as_dict(),
            "active_jobs": self.active_jobs,
            "round_trip_ms": self.round_trip_ms,
            "models": list(self.models),
            "evidence_ref": self.evidence_ref,
        }

    def digest(self) -> str:
        return _canonical_digest(self.as_dict())


@dataclass(frozen=True, slots=True)
class WorkloadRequest:
    request_id: str
    kind: WorkloadKind
    required_capabilities: frozenset[ComputeCapability] = frozenset()
    any_capabilities: frozenset[ComputeCapability] = frozenset()
    workspace_mapping: str | None = None
    min_free_ram_bytes: int = 0
    min_free_disk_bytes: int = 0
    min_free_vram_bytes: int = 0
    max_load_fraction: float | None = None
    local_only: bool = False
    allow_remote: bool = False
    allow_local_fallback: bool = False
    preferred_node_ids: frozenset[str] = frozenset()
    avoided_node_ids: frozenset[str] = frozenset()
    required_model: str | None = None
    idempotent: bool = False

    def __post_init__(self) -> None:
        _require_identity(self.request_id, "request_id")
        if not isinstance(self.kind, WorkloadKind):
            raise ValueError("kind must be a WorkloadKind")
        for values, label in (
            (self.required_capabilities, "required_capabilities"),
            (self.any_capabilities, "any_capabilities"),
        ):
            if any(not isinstance(item, ComputeCapability) for item in values):
                raise ValueError(f"{label} contains an unknown capability")
        if self.workspace_mapping is not None:
            _require_identity(self.workspace_mapping, "workspace_mapping")
        for name in (
            "min_free_ram_bytes",
            "min_free_disk_bytes",
            "min_free_vram_bytes",
        ):
            _optional_nonnegative_int(getattr(self, name), name)
        _optional_fraction(self.max_load_fraction, "max_load_fraction")
        for values, label in (
            (self.preferred_node_ids, "preferred_node_ids"),
            (self.avoided_node_ids, "avoided_node_ids"),
        ):
            for node_id in values:
                _require_identity(node_id, label)
        if self.preferred_node_ids & self.avoided_node_ids:
            raise ValueError("a node cannot be both preferred and avoided")
        if self.required_model is not None and (
            not isinstance(self.required_model, str)
            or not self.required_model
            or len(self.required_model) > 256
        ):
            raise ValueError("required_model must be bounded")
        if not all(isinstance(value, bool) for value in (
            self.local_only,
            self.allow_remote,
            self.allow_local_fallback,
            self.idempotent,
        )):
            raise ValueError("workload flags must be boolean")
        if self.local_only and self.allow_remote:
            raise ValueError("local_only and allow_remote cannot both be enabled")
        if self.allow_local_fallback and not self.allow_remote:
            raise ValueError("local fallback only applies to remote-enabled workloads")

    def as_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "kind": self.kind.value,
            "required_capabilities": sorted(item.value for item in self.required_capabilities),
            "any_capabilities": sorted(item.value for item in self.any_capabilities),
            "workspace_mapping": self.workspace_mapping,
            "min_free_ram_bytes": self.min_free_ram_bytes,
            "min_free_disk_bytes": self.min_free_disk_bytes,
            "min_free_vram_bytes": self.min_free_vram_bytes,
            "max_load_fraction": self.max_load_fraction,
            "local_only": self.local_only,
            "allow_remote": self.allow_remote,
            "allow_local_fallback": self.allow_local_fallback,
            "preferred_node_ids": sorted(self.preferred_node_ids),
            "avoided_node_ids": sorted(self.avoided_node_ids),
            "required_model": self.required_model,
            "idempotent": self.idempotent,
        }

    def digest(self) -> str:
        return _canonical_digest(self.as_dict())


@dataclass(frozen=True, slots=True)
class CandidateDecision:
    node_id: str
    eligible: bool
    reason_code: str
    score: float | None = None


@dataclass(frozen=True, slots=True)
class PlacementDecision:
    request_digest: str
    selected_node_id: str | None
    candidates: tuple[CandidateDecision, ...]
    ranked_node_ids: tuple[str, ...]
    snapshot_digests: tuple[tuple[str, str], ...]


class ComputePlacementScheduler:
    """Apply hard constraints, then rank eligible whole-job destinations."""

    def __init__(self, *, snapshot_ttl: timedelta = timedelta(seconds=30)) -> None:
        if not isinstance(snapshot_ttl, timedelta) or snapshot_ttl <= timedelta(0):
            raise ValueError("snapshot_ttl must be positive")
        if snapshot_ttl > timedelta(hours=1):
            raise ValueError("snapshot_ttl must be at most one hour")
        self.snapshot_ttl = snapshot_ttl

    def place(
        self,
        request: WorkloadRequest,
        snapshots: Iterable[NodeSnapshot],
        *,
        now: datetime,
    ) -> PlacementDecision:
        if not isinstance(request, WorkloadRequest):
            raise TypeError("request must be a WorkloadRequest")
        if request.kind is WorkloadKind.INFERENCE:
            raise ValueError("inference placement remains owned by the model gateway")
        current = _aware_utc(now, "now")
        ordered = sorted(tuple(snapshots), key=lambda item: item.node.node_id)
        node_ids = [item.node.node_id for item in ordered]
        if len(node_ids) != len(set(node_ids)):
            raise ValueError("snapshots contain duplicate node identities")

        decisions: list[CandidateDecision] = []
        eligible: list[CandidateDecision] = []
        digests: list[tuple[str, str]] = []
        for snapshot in ordered:
            digests.append((snapshot.node.node_id, snapshot.digest()))
            decision = self._assess(request, snapshot, current)
            decisions.append(decision)
            if decision.eligible:
                eligible.append(decision)
        ranked = tuple(
            item.node_id
            for item in sorted(
                eligible,
                key=lambda item: (-(item.score or 0.0), item.node_id),
            )
        )
        return PlacementDecision(
            request_digest=request.digest(),
            selected_node_id=ranked[0] if ranked else None,
            candidates=tuple(decisions),
            ranked_node_ids=ranked,
            snapshot_digests=tuple(digests),
        )

    def _assess(
        self,
        request: WorkloadRequest,
        snapshot: NodeSnapshot,
        now: datetime,
    ) -> CandidateDecision:
        node = snapshot.node

        def reject(reason: str) -> CandidateDecision:
            return CandidateDecision(node.node_id, False, reason)

        if node.node_id in request.avoided_node_ids:
            return reject("node_avoided")
        if snapshot.health is not NodeHealth.HEALTHY:
            return reject("unhealthy")
        observed = _aware_utc(snapshot.observed_at, "observed_at")
        if observed > now + timedelta(seconds=5):
            return reject("future_observation")
        if now - observed > self.snapshot_ttl:
            return reject("stale")
        if request.kind not in snapshot.effective_workloads:
            return reject("workload_not_allowed")
        if request.local_only and not node.local:
            return reject("local_only")
        if not node.local and not request.allow_remote:
            return reject("remote_not_allowed")
        if not request.required_capabilities <= snapshot.effective_capabilities:
            return reject("missing_capability")
        if request.any_capabilities and not (
            request.any_capabilities & snapshot.effective_capabilities
        ):
            return reject("missing_any_capability")
        if request.workspace_mapping and request.workspace_mapping not in node.workspace_mappings:
            return reject("workspace_unavailable")
        if request.required_model and request.required_model not in snapshot.models:
            return reject("model_unavailable")

        resource_reason = self._resource_rejection(request, snapshot.resources)
        if resource_reason:
            return reject(resource_reason)

        score = float(node.preference_weight)
        if node.node_id in request.preferred_node_ids:
            score += 100.0
        if snapshot.resources.free_ram_bytes is not None:
            score += min(snapshot.resources.free_ram_bytes / (1 << 30), 256.0) * 0.1
        if snapshot.resources.free_disk_bytes is not None:
            score += min(snapshot.resources.free_disk_bytes / (1 << 30), 4096.0) * 0.001
        if snapshot.resources.free_vram_bytes is not None:
            score += min(snapshot.resources.free_vram_bytes / (1 << 30), 256.0) * 0.2
        if snapshot.resources.load_fraction is not None:
            score -= snapshot.resources.load_fraction * 20.0
        score -= snapshot.active_jobs * 5.0
        if snapshot.round_trip_ms is not None:
            score -= min(snapshot.round_trip_ms, 60_000.0) * 0.001
        if request.required_model and request.required_model in snapshot.models:
            score += 20.0
        return CandidateDecision(node.node_id, True, "eligible", round(score, 9))

    @staticmethod
    def _resource_rejection(
        request: WorkloadRequest,
        resources: NodeResources,
    ) -> str | None:
        for floor, value, unknown, insufficient in (
            (
                request.min_free_ram_bytes,
                resources.free_ram_bytes,
                "ram_unknown",
                "insufficient_ram",
            ),
            (
                request.min_free_disk_bytes,
                resources.free_disk_bytes,
                "disk_unknown",
                "insufficient_disk",
            ),
            (
                request.min_free_vram_bytes,
                resources.free_vram_bytes,
                "vram_unknown",
                "insufficient_vram",
            ),
        ):
            if floor > 0 and value is None:
                return unknown
            if floor > 0 and value is not None and value < floor:
                return insufficient
        if request.max_load_fraction is not None:
            if resources.load_fraction is None:
                return "load_unknown"
            if resources.load_fraction > request.max_load_fraction:
                return "load_too_high"
        return None


__all__ = [
    "CandidateDecision",
    "ComputeCapability",
    "ComputeNode",
    "ComputePlacementScheduler",
    "NodeHealth",
    "NodeResources",
    "NodeSnapshot",
    "PlacementDecision",
    "WorkloadKind",
    "WorkloadRequest",
]
