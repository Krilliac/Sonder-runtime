"""Fail-closed shard-loss and replacement decisions.

This module describes the deployment-level decision only.  It does not talk to
a model provider, copy KV caches, mutate reservations, or replay application
requests.  A ``READY`` plan means that a separately integrated provider may
attempt replacement after the returned manifest and evidence are rechecked;
application replay remains explicitly required.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import re

from .model_conformance import ConformanceResult
from .model_deployment import ModelDeployment, ModelRank


_IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_MAX_SHARDS = 256


class ShardRecoveryError(ValueError):
    """Malformed deployment or replacement evidence."""


class RecoveryState(StrEnum):
    """Decision state returned before any provider side effect."""

    READY = "ready"
    PAUSED = "paused"


def _identity(value: object, field: str) -> str:
    if not isinstance(value, str) or _IDENTITY.fullmatch(value) is None:
        raise ShardRecoveryError(f"{field} must be a bounded stable identity")
    return value


def _digest(value: object, field: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ShardRecoveryError(f"{field} must be a canonical SHA-256 digest")
    return value


def _capacity(value: object) -> int:
    if type(value) is not int or not 1 <= value <= (1 << 24):
        raise ShardRecoveryError("capacity_tokens must be within 1..16777216")
    return value


@dataclass(frozen=True, slots=True)
class ShardRecoveryCandidate:
    """Provider-reported replacement capacity bound to the deployment files."""

    rank: int
    host_id: str
    worker_id: str
    device_id: str
    backend_id: str
    backend_digest: str
    model_bundle_digest: str
    runtime_config_digest: str
    reservation_group: str
    capacity_tokens: int
    healthy: bool

    def __post_init__(self) -> None:
        if type(self.rank) is not int or not 0 <= self.rank < _MAX_SHARDS:
            raise ShardRecoveryError("rank must be within 0..255")
        for field in ("host_id", "worker_id", "device_id", "backend_id", "reservation_group"):
            object.__setattr__(self, field, _identity(getattr(self, field), field))
        for field in ("backend_digest", "model_bundle_digest", "runtime_config_digest"):
            object.__setattr__(self, field, _digest(getattr(self, field), field))
        object.__setattr__(self, "capacity_tokens", _capacity(self.capacity_tokens))
        if type(self.healthy) is not bool:
            raise ShardRecoveryError("healthy must be boolean")

    @property
    def physical_device(self) -> tuple[str, str]:
        return self.host_id, self.device_id

    def as_rank(self) -> ModelRank:
        return ModelRank(self.rank, self.host_id, self.worker_id, self.device_id)


@dataclass(frozen=True, slots=True)
class ShardRecoveryDecision:
    """Bounded replacement plan with explicit replay/cache limitations."""

    state: RecoveryState
    reason: str
    deployment_digest: str
    lost_ranks: tuple[int, ...]
    replacement_ranks: tuple[ModelRank, ...] = ()
    kv_cache_recovery_verified: bool = False
    application_replay_required: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "deployment_digest", _digest(self.deployment_digest, "deployment_digest"))
        if not isinstance(self.state, RecoveryState):
            raise ShardRecoveryError("state must be a RecoveryState")
        if not isinstance(self.reason, str) or not self.reason:
            raise ShardRecoveryError("reason is required")
        if type(self.lost_ranks) is not tuple or not self.lost_ranks:
            raise ShardRecoveryError("lost_ranks must be a non-empty tuple")
        if len(self.lost_ranks) > _MAX_SHARDS or any(type(rank) is not int or not 0 <= rank < _MAX_SHARDS for rank in self.lost_ranks):
            raise ShardRecoveryError("lost_ranks are outside the bounded shard range")
        if len(set(self.lost_ranks)) != len(self.lost_ranks):
            raise ShardRecoveryError("lost_ranks must not contain duplicates")
        if type(self.replacement_ranks) is not tuple:
            raise ShardRecoveryError("replacement_ranks must be an immutable tuple")
        if any(not isinstance(rank, ModelRank) for rank in self.replacement_ranks):
            raise ShardRecoveryError("replacement_ranks must contain ModelRank values")
        replacement_ids = tuple(rank.rank for rank in self.replacement_ranks)
        if len(set(replacement_ids)) != len(replacement_ids):
            raise ShardRecoveryError("replacement_ranks must not contain duplicates")
        if self.state is RecoveryState.READY and replacement_ids != self.lost_ranks:
            raise ShardRecoveryError("ready recovery must replace exactly the lost ranks")
        if self.state is RecoveryState.PAUSED and self.replacement_ranks:
            raise ShardRecoveryError("paused recovery cannot publish replacement ranks")
        if type(self.kv_cache_recovery_verified) is not bool or type(self.application_replay_required) is not bool:
            raise ShardRecoveryError("recovery flags must be boolean")
        if self.kv_cache_recovery_verified:
            raise ShardRecoveryError("KV cache recovery cannot be asserted by this contract")
        if not self.application_replay_required:
            raise ShardRecoveryError("application replay remains required")


def _paused(deployment: ModelDeployment, lost_ranks: tuple[int, ...], reason: str) -> ShardRecoveryDecision:
    return ShardRecoveryDecision(RecoveryState.PAUSED, reason, deployment.digest, lost_ranks)


def plan_shard_replacement(
    deployment: ModelDeployment,
    *,
    lost_ranks: tuple[int, ...],
    candidates: tuple[ShardRecoveryCandidate, ...],
    conformance: ConformanceResult | None,
    deployment_digest: str | None = None,
) -> ShardRecoveryDecision:
    """Return a replacement plan without invoking a provider.

    Every candidate must carry the exact backend, model, runtime, reservation,
    rank, and capacity identity of the deployment.  A missing or stale input
    returns ``PAUSED`` so callers cannot accidentally promote a partial plan.
    """
    if not isinstance(deployment, ModelDeployment):
        raise ShardRecoveryError("model deployment is required")
    if deployment_digest is not None and _digest(deployment_digest, "deployment_digest") != deployment.digest:
        raise ShardRecoveryError("deployment_digest does not match the manifest")
    if type(lost_ranks) is not tuple or not 1 <= len(lost_ranks) <= len(deployment.ranks):
        raise ShardRecoveryError("lost_ranks must be a bounded non-empty tuple")
    if len(set(lost_ranks)) != len(lost_ranks) or any(type(rank) is not int or not 0 <= rank < len(deployment.ranks) for rank in lost_ranks):
        raise ShardRecoveryError("lost_ranks must name unique manifest ranks")
    if type(candidates) is not tuple or len(candidates) > _MAX_SHARDS:
        raise ShardRecoveryError("candidates must be a bounded tuple")
    if any(not isinstance(candidate, ShardRecoveryCandidate) for candidate in candidates):
        raise ShardRecoveryError("candidates must contain ShardRecoveryCandidate values")
    lost = tuple(sorted(lost_ranks))
    if not deployment.is_multihost:
        return _paused(deployment, lost, "single_host_deployment_has_no_distributed_recovery")
    if not isinstance(conformance, ConformanceResult) or not conformance.accepted or not conformance.distributed:
        return _paused(deployment, lost, "distributed_backend_conformance_required")
    if len(candidates) != len(lost):
        return _paused(deployment, lost, "replacement_candidate_count_mismatch")
    by_rank = {candidate.rank: candidate for candidate in candidates}
    if len(by_rank) != len(candidates):
        return _paused(deployment, lost, "replacement_candidate_ranks_duplicated")
    if set(by_rank) != set(lost):
        return _paused(deployment, lost, "candidate_rank_mismatch")
    survivors = {
        (assignment.host_id, assignment.device_id)
        for index, assignment in enumerate(deployment.ranks)
        if index not in lost
    }
    candidate_devices = set()
    for rank in lost:
        candidate = by_rank[rank]
        if not candidate.healthy:
            return _paused(deployment, lost, "candidate_unhealthy")
        if candidate.backend_id != deployment.backend:
            return _paused(deployment, lost, "candidate_backend_mismatch")
        if candidate.backend_digest != deployment.backend_digest:
            return _paused(deployment, lost, "candidate_backend_artifact_mismatch")
        if candidate.model_bundle_digest != deployment.model_bundle_digest or candidate.runtime_config_digest != deployment.runtime_config_digest:
            return _paused(deployment, lost, "candidate_artifact_mismatch")
        if candidate.reservation_group != deployment.reservation_group:
            return _paused(deployment, lost, "candidate_reservation_group_mismatch")
        if candidate.capacity_tokens < deployment.context_tokens:
            return _paused(deployment, lost, "candidate_capacity_insufficient")
        if candidate.physical_device in survivors:
            return _paused(deployment, lost, "candidate_device_conflicts_with_survivor")
        if candidate.physical_device in candidate_devices:
            return _paused(deployment, lost, "candidate_devices_duplicated")
        candidate_devices.add(candidate.physical_device)
    replacements = tuple(by_rank[rank].as_rank() for rank in lost)
    return ShardRecoveryDecision(RecoveryState.READY, "replacement_manifest_ready", deployment.digest, lost, replacements)


__all__ = [
    "RecoveryState",
    "ShardRecoveryCandidate",
    "ShardRecoveryDecision",
    "ShardRecoveryError",
    "plan_shard_replacement",
]
