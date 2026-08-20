from __future__ import annotations

import pytest

from sonder_runtime.application.execution.containment import (
    CapabilityHealth,
    ContainerCapability,
    ContainerEngine,
    ContainmentStatus,
    GuardedContainerContract,
    GuardedContainerPolicy,
    RemoteWorkerBoundary,
    RemoteWorkerCapability,
    failure_isolation_claim,
)
from sonder_runtime.application.execution.world_control import IsolationTruth


def test_default_guarded_container_policy_is_hardened_and_digest_bound() -> None:
    policy = GuardedContainerPolicy()
    assert policy.network_enabled is False
    assert policy.read_only_root is True
    assert policy.no_new_privileges is True
    assert policy.drop_all_capabilities is True
    assert policy.non_root_user is True
    assert policy.implicit_image_pull is False

    rejected = GuardedContainerContract().assess(
        ContainerCapability(ContainerEngine.DOCKER, CapabilityHealth.HEALTHY, "27.0"),
        image_digest=None,
    )
    assert rejected.status is ContainmentStatus.REJECTED
    assert rejected.isolation.truth is IsolationTruth.UNVERIFIED


def test_container_requires_healthy_versioned_engine_and_never_fakes_security() -> None:
    contract = GuardedContainerContract()
    unavailable = contract.assess(
        ContainerCapability(ContainerEngine.PODMAN, CapabilityHealth.UNKNOWN, None),
        image_digest="sha256:image",
    )
    assert unavailable.status is ContainmentStatus.REJECTED
    assert unavailable.security_boundary_verified is False

    accepted = contract.assess(
        ContainerCapability(ContainerEngine.PODMAN, CapabilityHealth.HEALTHY, "5.0"),
        image_digest="sha256:image",
    )
    assert accepted.status is ContainmentStatus.ACCEPTED
    assert accepted.isolation.truth is IsolationTruth.FAILURE_ISOLATION_ONLY
    assert dict(accepted.controls)["network"] == "disabled"


def test_remote_worker_requires_external_health_and_declared_world_capability() -> None:
    boundary = RemoteWorkerBoundary()
    unhealthy = boundary.assess(
        RemoteWorkerCapability("worker-1", "https://worker.invalid"),
    )
    assert unhealthy.status is ContainmentStatus.REJECTED
    assert unhealthy.isolation.truth is IsolationTruth.UNVERIFIED

    mismatch = boundary.assess(
        RemoteWorkerCapability(
            "worker-1",
            "https://worker.invalid",
            CapabilityHealth.HEALTHY,
            frozenset({"code"}),
        ),
    )
    assert mismatch.status is ContainmentStatus.REJECTED

    healthy = boundary.assess(
        RemoteWorkerCapability(
            "worker-1",
            "https://worker.invalid",
            CapabilityHealth.HEALTHY,
            frozenset({"remote"}),
        ),
    )
    assert healthy.status is ContainmentStatus.ACCEPTED
    assert healthy.isolation.truth is IsolationTruth.FAILURE_ISOLATION_ONLY


def test_explicit_evidence_is_required_for_security_boundary_claim() -> None:
    container = GuardedContainerContract().assess(
        ContainerCapability(
            ContainerEngine.DOCKER,
            CapabilityHealth.HEALTHY,
            "27.0",
            evidence_ref="attestation:container-1",
        ),
        image_digest="sha256:image",
    )
    remote = RemoteWorkerBoundary().assess(
        RemoteWorkerCapability(
            "worker-2",
            "https://worker.invalid",
            CapabilityHealth.HEALTHY,
            frozenset({"remote"}),
            evidence_ref="attestation:worker-2",
        ),
    )
    assert container.security_boundary_verified is True
    assert remote.security_boundary_verified is True


def test_failure_isolation_does_not_claim_a_sandbox() -> None:
    claim = failure_isolation_claim("child failure cannot corrupt parent lifecycle")
    assert claim.truth is IsolationTruth.FAILURE_ISOLATION_ONLY
    assert claim.is_security_boundary is False


def test_guarded_policy_rejects_weakened_defaults() -> None:
    with pytest.raises(ValueError):
        GuardedContainerPolicy(network_enabled=True)
    with pytest.raises(ValueError):
        GuardedContainerPolicy(read_only_root=False)
