from __future__ import annotations

import pytest

from sonder_runtime.adapters.execution import (
    ConfiguredRemoteWorkerProvider,
    WorldCapability,
    WorldUnavailable,
    default_container_provider,
)
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.execution.world_control import IsolationTruth
from sonder_runtime.application.ports import SandboxWorldKind, SandboxWorldSpec
from sonder_runtime.application.ports.execution_world import ShellRequest


def context():
    return local_owner_context(correlation_id="seam-004-integration")


def test_sandbox_provider_exposes_one_typed_fail_closed_world_owner():
    sandbox = default_container_provider(image="sonder:guarded").provision(
        SandboxWorldSpec("container-1", SandboxWorldKind.CONTAINER), context()
    )
    world = sandbox.execution_world

    assert world.spec.world_id == sandbox.spec.world_id
    assert world.identity.world_id == world.spec.world_id
    assert world.capabilities == frozenset(WorldCapability)
    assert world.isolation.truth is IsolationTruth.FAILURE_ISOLATION_ONLY
    assert not world.isolation.is_security_boundary
    assert world.snapshot().active_resources == 0

    with pytest.raises(WorldUnavailable, match="no verified execution transport"):
        world.shell.execute(ShellRequest("echo unsafe"), context())


def test_declared_capabilities_are_checked_before_unsupported_transport():
    sandbox = ConfiguredRemoteWorkerProvider(
        endpoint="https://worker.example/run",
        worker_id="worker-1",
        capabilities=frozenset({WorldCapability.CODE}),
    ).provision(
        SandboxWorldSpec(
            "remote-1",
            SandboxWorldKind.REMOTE,
            endpoint="https://worker.example/run",
        ),
        context(),
    )

    with pytest.raises(WorldUnavailable, match="not declared"):
        sandbox.execution_world.shell.execute(ShellRequest("echo remote"), context())


def test_cleanup_is_the_lifecycle_barrier_and_remains_idempotent():
    world = default_container_provider(image="sonder:guarded").provision(
        SandboxWorldSpec("container-1", SandboxWorldKind.CONTAINER), context()
    ).execution_world

    assert world.cancel(reason="shutdown")
    first = world.cleanup(timeout=0)
    second = world.cleanup(timeout=0)
    assert first.quiescent and first.active_resources == 0
    assert second.quiescent and second.active_resources == 0
    with pytest.raises(WorldUnavailable, match="quiescent"):
        world.shell.execute(ShellRequest("echo after cleanup"), context())
