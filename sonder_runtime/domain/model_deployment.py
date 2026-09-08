"""Immutable identity for one model replica, never proof of backend readiness.

Adapters must verify artifact digests and physical device identities. A valid
manifest does not reserve capacity or authorize a model/network operation.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import re


def _identity(value: str, name: str) -> None:
    if not isinstance(value, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value
    ):
        raise ValueError(f"{name} must be a bounded stable identity")


def _integer(value: int, name: str, minimum: int, maximum: int) -> None:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer within {minimum}..{maximum}")


@dataclass(frozen=True, slots=True)
class ModelRank:
    rank: int
    host_id: str
    worker_id: str
    device_id: str

    def __post_init__(self) -> None:
        _integer(self.rank, "rank", 0, 255)
        for name in ("host_id", "worker_id", "device_id"):
            _identity(getattr(self, name), name)


@dataclass(frozen=True, slots=True)
class ModelDeployment:
    cluster_id: str
    deployment_id: str
    revision: int
    backend: str
    backend_digest: str
    model_bundle_digest: str
    runtime_config_digest: str
    context_tokens: int
    tensor_parallel: int
    pipeline_parallel: int
    reservation_group: str
    ranks: tuple[ModelRank, ...]

    def __post_init__(self) -> None:
        for name in ("cluster_id", "deployment_id", "backend", "reservation_group"):
            _identity(getattr(self, name), name)
        for name in ("backend_digest", "model_bundle_digest", "runtime_config_digest"):
            value = getattr(self, name)
            if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
                raise ValueError(f"{name} must be a canonical SHA-256 digest")
        _integer(self.revision, "revision", 1, (1 << 63) - 1)
        _integer(self.context_tokens, "context_tokens", 1, 1 << 24)
        _integer(self.tensor_parallel, "tensor_parallel", 1, 256)
        _integer(self.pipeline_parallel, "pipeline_parallel", 1, 256)
        if type(self.ranks) is not tuple or not 1 <= len(self.ranks) <= 256:
            raise ValueError("ranks must be an immutable tuple of 1..256 assignments")
        if len(self.ranks) != self.tensor_parallel * self.pipeline_parallel:
            raise ValueError("rank count must match tensor and pipeline topology")
        devices = set()
        for rank, assignment in enumerate(self.ranks):
            if type(assignment) is not ModelRank or assignment.rank != rank:
                raise ValueError("ranks must be exact contiguous ordered assignments")
            device = (assignment.host_id, assignment.device_id)
            if device in devices:
                raise ValueError("one physical device cannot appear in multiple ranks")
            devices.add(device)

    @property
    def digest(self) -> str:
        payload = {"schema": "sonder.model-deployment.v1", "manifest": asdict(self)}
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
        return hashlib.sha256(encoded).hexdigest()

    @property
    def is_multihost(self) -> bool:
        """Configured topology only; this does not attest actual distribution."""
        return len({assignment.host_id for assignment in self.ranks}) > 1
