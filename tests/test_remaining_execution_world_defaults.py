from __future__ import annotations

import pytest

from sonder_runtime.adapters.execution import (
    ConfiguredRemoteWorkerProvider,
    WorldCapability,
    WorldUnavailable,
    default_container_provider,
)
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports import SandboxWorldKind, SandboxWorldSpec, SandboxWorldState
from sonder_runtime.application.ports.execution_world import ShellRequest


def context():
    return local_owner_context(correlation_id="exec-world-test")


def test_default_container_is_guarded_and_has_stable_identity():
    provider = default_container_provider(image="sonder:guarded")
    world = provider.provision(
        SandboxWorldSpec("container-1", SandboxWorldKind.CONTAINER), context()
    )
    assert world.identity.provider_id == "container-default"
    assert world.identity.kind is SandboxWorldKind.CONTAINER
    assert world.execution_world.identity.world_id == "container-1"
    assert WorldCapability.CODE in world.capabilities


def test_default_provider_rejects_non_container_and_never_falls_back():
    provider = default_container_provider(image="sonder:guarded")
    with pytest.raises(WorldUnavailable, match="container worlds only"):
        provider.provision(SandboxWorldSpec("local-1", SandboxWorldKind.LOCAL), context())
    world = provider.provision(
        SandboxWorldSpec("container-1", SandboxWorldKind.CONTAINER), context()
    )
    with pytest.raises(WorldUnavailable, match="no verified execution transport"):
        world.execution_world.shell.execute(ShellRequest("echo unsafe"), context())


def test_remote_worker_requires_configured_https_identity_and_capability():
    with pytest.raises(ValueError, match="HTTPS"):
        ConfiguredRemoteWorkerProvider(
            endpoint="http://worker.example", worker_id="worker-1",
            capabilities=frozenset({WorldCapability.CODE}),
        )
    provider = ConfiguredRemoteWorkerProvider(
        endpoint="https://worker.example/run",
        worker_id="worker-1",
        capabilities=frozenset({WorldCapability.CODE, WorldCapability.SHELL}),
    )
    with pytest.raises(WorldUnavailable, match="configured endpoint"):
        provider.provision(
            SandboxWorldSpec("remote-1", SandboxWorldKind.REMOTE, endpoint="https://other/run"),
            context(),
        )
    world = provider.provision(
        SandboxWorldSpec("remote-1", SandboxWorldKind.REMOTE, endpoint="https://worker.example/run"),
        context(),
    )
    assert world.identity.worker_id == "worker-1"
    assert world.identity.endpoint == "https://worker.example/run"
    assert world.capabilities == frozenset({WorldCapability.CODE, WorldCapability.SHELL})


def test_remote_boundary_fails_closed_and_cleanup_proves_quiescence():
    provider = ConfiguredRemoteWorkerProvider(
        endpoint="https://worker.example/run", worker_id="worker-1",
        capabilities=frozenset({WorldCapability.CODE}),
    )
    world = provider.provision(
        SandboxWorldSpec("remote-1", SandboxWorldKind.REMOTE, endpoint="https://worker.example/run"),
        context(),
    )
    with pytest.raises(WorldUnavailable):
        world.execution_world.shell.execute(ShellRequest("echo remote"), context())
    assert world.cleanup(timeout=0).quiescent
    assert world.snapshot().state is SandboxWorldState.QUIESCENT
