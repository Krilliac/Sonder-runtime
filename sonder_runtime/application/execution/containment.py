"""Fail-closed containment and remote-worker boundary contracts.

This module deliberately does not start containers, spawn processes, or make
network calls.  It turns provider capability and health observations into a
bounded decision that an adapter can execute.  In particular, a configured
container is not called a security boundary merely because it is named
``container`` and a remote worker is not considered healthy merely because an
endpoint was configured.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Mapping

from .world_control import IsolationClaim, IsolationTruth

logger = logging.getLogger(__name__)


class ContainerEngine(StrEnum):
    DOCKER = "docker"
    PODMAN = "podman"


class CapabilityHealth(StrEnum):
    UNKNOWN = "unknown"
    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"


class ContainmentStatus(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"


@dataclass(frozen=True)
class GuardedContainerPolicy:
    """The non-negotiable defaults for generated/guarded execution."""

    network_enabled: bool = False
    read_only_root: bool = True
    no_new_privileges: bool = True
    drop_all_capabilities: bool = True
    non_root_user: bool = True
    implicit_image_pull: bool = False
    memory_bytes: int = 512 * 1024 * 1024
    cpu_seconds: float = 30.0
    pids_limit: int = 128

    def __post_init__(self) -> None:
        if self.network_enabled:
            logger.critical("security violation: guarded container constructed with networking enabled")
            raise ValueError("guarded container networking must be disabled")
        if self.implicit_image_pull:
            logger.critical("security violation: guarded container constructed with implicit image pull enabled")
            raise ValueError("guarded containers cannot implicitly pull images")
        if not self.read_only_root or not self.no_new_privileges:
            logger.critical("security violation: guarded container hardening constraints bypassed")
            raise ValueError("guarded container hardening cannot be disabled")
        if not self.drop_all_capabilities or not self.non_root_user:
            logger.critical("security violation: guarded container privilege reduction requirements not met")
            raise ValueError("guarded container privilege reduction is required")
        if self.memory_bytes < 1 or self.cpu_seconds <= 0 or self.pids_limit < 1:
            raise ValueError("container resource limits must be positive")


@dataclass(frozen=True)
class ContainerCapability:
    engine: ContainerEngine | None
    health: CapabilityHealth = CapabilityHealth.UNKNOWN
    version: str | None = None
    evidence_ref: str | None = None

    @property
    def usable(self) -> bool:
        return (
            self.engine is not None
            and self.health is CapabilityHealth.HEALTHY
            and bool(self.version and self.version.strip())
        )


@dataclass(frozen=True)
class RemoteWorkerCapability:
    """An observation about a configured worker, not a network client."""

    worker_id: str
    endpoint: str | None
    health: CapabilityHealth = CapabilityHealth.UNKNOWN
    supported_worlds: frozenset[str] = frozenset()
    evidence_ref: str | None = None

    def __post_init__(self) -> None:
        if not self.worker_id.strip():
            raise ValueError("remote worker id must be non-empty")

    @property
    def usable(self) -> bool:
        return bool(
            self.endpoint
            and self.endpoint.strip()
            and self.health is CapabilityHealth.HEALTHY
        )


@dataclass(frozen=True)
class ContainmentDecision:
    status: ContainmentStatus
    world_kind: str
    isolation: IsolationClaim
    reason: str
    provider_id: str | None = None
    controls: tuple[tuple[str, str], ...] = ()

    @property
    def accepted(self) -> bool:
        return self.status is ContainmentStatus.ACCEPTED

    @property
    def security_boundary_verified(self) -> bool:
        return self.isolation.truth is IsolationTruth.SECURITY_BOUNDARY_VERIFIED


def _claim_for(
    *,
    evidence_ref: str | None,
    accepted: bool,
    rationale: str,
) -> IsolationClaim:
    """Choose the strongest truthful claim supported by the observation."""

    if evidence_ref and evidence_ref.strip():
        truth = IsolationTruth.SECURITY_BOUNDARY_VERIFIED
    elif accepted:
        truth = IsolationTruth.FAILURE_ISOLATION_ONLY
    else:
        truth = IsolationTruth.UNVERIFIED
    return IsolationClaim(truth, rationale, evidence_ref)


class GuardedContainerContract:
    """Default container admission boundary.

    ``assess`` is pure and fail-closed.  A caller must provide a healthy,
    versioned engine observation; otherwise no container execution is
    admitted.  The contract never invents evidence or upgrades a claim.
    """

    def __init__(self, policy: GuardedContainerPolicy | None = None) -> None:
        self.policy = policy or GuardedContainerPolicy()

    def assess(
        self,
        capability: ContainerCapability,
        *,
        provider_id: str = "container",
        image_digest: str | None = None,
    ) -> ContainmentDecision:
        logger.debug(f"GuardedContainerContract.assess: engine={capability.engine!r}, health={capability.health.value!r}, provider_id={provider_id!r}, has_digest={bool(image_digest)}")
        logger.info(f"container containment assessment starting: engine={capability.engine!r}, health={capability.health.value!r}, provider_id={provider_id!r}")
        if not image_digest or not image_digest.strip():
            logger.error(f"container admission rejected: missing image digest, provider_id={provider_id!r}")
            logger.warning(f"container admission rejected: missing image digest, provider_id={provider_id!r}")
            return ContainmentDecision(
                ContainmentStatus.REJECTED,
                "container",
                _claim_for(
                    evidence_ref=None,
                    accepted=False,
                    rationale="container image digest is required before admission",
                ),
                "container image must be immutable and digest-bound",
                provider_id,
            )
        if not capability.usable:
            logger.error(f"container capability unusable: engine={capability.engine!r}, health={capability.health.value!r}, provider_id={provider_id!r}")
            logger.warning(f"container capability unusable: engine={capability.engine!r}, health={capability.health.value!r}, version={capability.version!r}, provider_id={provider_id!r}")
            return ContainmentDecision(
                ContainmentStatus.REJECTED,
                "container",
                _claim_for(
                    evidence_ref=None,
                    accepted=False,
                    rationale="container capability is unavailable or unhealthy",
                ),
                "healthy versioned container engine is required",
                provider_id,
            )
        controls = tuple(
            (name, value)
            for name, value in (
                ("network", "disabled"),
                ("root_filesystem", "read_only"),
                ("privileges", "dropped"),
                ("user", "non_root"),
                ("pull", "disabled"),
            )
        )
        return ContainmentDecision(
            ContainmentStatus.ACCEPTED,
            "container",
            _claim_for(
                evidence_ref=capability.evidence_ref,
                accepted=True,
                rationale="guarded container controls are requested; security strength follows evidence",
            ),
            "container admission accepted",
            provider_id,
            controls,
        )


class RemoteWorkerBoundary:
    """Admission and truth boundary for a configured remote worker.

    This class intentionally performs no endpoint probing.  Health must come
    from an independently owned adapter, and isolation remains unverified
    unless that adapter supplies explicit evidence.
    """

    def assess(
        self,
        capability: RemoteWorkerCapability,
        *,
        requested_world: str = "remote",
    ) -> ContainmentDecision:
        logger.debug(f"RemoteWorkerBoundary.assess: worker_id={capability.worker_id!r}, health={capability.health.value!r}, requested_world={requested_world!r}, usable={capability.usable}")
        logger.info(f"remote worker assessment starting: worker_id={capability.worker_id!r}, health={capability.health.value!r}, usable={capability.usable}")
        if not capability.usable:
            logger.error(f"remote worker admission rejected: worker_id={capability.worker_id!r}, health={capability.health.value!r}, endpoint_configured={bool(capability.endpoint)}")
            logger.warning(f"remote worker unhealthy: worker_id={capability.worker_id!r}, health={capability.health.value!r}, endpoint_configured={bool(capability.endpoint)}")
            return ContainmentDecision(
                ContainmentStatus.REJECTED,
                "remote",
                _claim_for(
                    evidence_ref=None,
                    accepted=False,
                    rationale="remote worker health and endpoint capability are not verified",
                ),
                "configured remote worker is not healthy",
                capability.worker_id,
            )
        if capability.supported_worlds and requested_world not in capability.supported_worlds:
            logger.error(f"remote worker capability mismatch: worker_id={capability.worker_id!r}, requested_world={requested_world!r}")
            logger.warning(f"remote worker capability mismatch: worker_id={capability.worker_id!r}, requested_world={requested_world!r}, supported_worlds={capability.supported_worlds!r}")
            return ContainmentDecision(
                ContainmentStatus.REJECTED,
                "remote",
                _claim_for(
                    evidence_ref=None,
                    accepted=False,
                    rationale="remote worker does not advertise the requested world",
                ),
                "remote worker capability mismatch",
                capability.worker_id,
            )
        return ContainmentDecision(
            ContainmentStatus.ACCEPTED,
            "remote",
            _claim_for(
                evidence_ref=capability.evidence_ref,
                accepted=True,
                rationale="remote worker health is reported by an external capability adapter",
            ),
            "remote worker admission accepted",
            capability.worker_id,
        )


def failure_isolation_claim(reason: str) -> IsolationClaim:
    """Create the honest claim for ordinary adapter failure containment."""

    return IsolationClaim(IsolationTruth.FAILURE_ISOLATION_ONLY, reason)


__all__ = [
    "CapabilityHealth",
    "ContainerCapability",
    "ContainerEngine",
    "ContainmentDecision",
    "ContainmentStatus",
    "GuardedContainerContract",
    "GuardedContainerPolicy",
    "RemoteWorkerBoundary",
    "RemoteWorkerCapability",
    "failure_isolation_claim",
]
