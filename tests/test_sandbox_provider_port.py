from __future__ import annotations

import pytest

from sonder_runtime.application.ports import (
    SandboxCleanupResult,
    SandboxPolicy,
    SandboxProvider,
    SandboxWorld,
    SandboxWorldKind,
    SandboxWorldSnapshot,
    SandboxWorldSpec,
    SandboxWorldState,
)


def test_contract_defines_all_world_kinds_and_policy_is_immutable():
    assert {kind.value for kind in SandboxWorldKind} == {
        "local", "container", "remote", "read_only"
    }
    policy = SandboxPolicy(allow_network=True, egress_hosts=("pypi.org",))
    assert policy.allow_network and policy.egress_hosts == ("pypi.org",)
    with pytest.raises(AttributeError):
        policy.allow_network = False  # type: ignore[misc]


def test_read_only_world_cannot_widen_write_or_persistence_authority():
    with pytest.raises(ValueError, match="read-only policy"):
        SandboxPolicy(read_only=True, allow_write=True)
    with pytest.raises(ValueError, match="read-only world"):
        SandboxWorldSpec("ro", SandboxWorldKind.READ_ONLY)

    spec = SandboxWorldSpec(
        "ro",
        SandboxWorldKind.READ_ONLY,
        SandboxPolicy(read_only=True, allow_write=False),
    )
    assert spec.policy.read_only and not spec.policy.allow_write


def test_policy_rejects_egress_hosts_when_network_is_disabled():
    with pytest.raises(ValueError, match="allow_network"):
        SandboxPolicy(egress_hosts=("example.test",))


class _World:
    def __init__(self):
        self.spec = SandboxWorldSpec("container-1", SandboxWorldKind.CONTAINER)
        self.execution_world = object()

    def cancel(self, *, reason: str = "cancellation requested") -> bool:
        return reason == "stop"

    def cleanup(self, timeout: float | None = None) -> SandboxCleanupResult:
        return SandboxCleanupResult(True, 0, SandboxWorldState.QUIESCENT)

    def snapshot(self) -> SandboxWorldSnapshot:
        return SandboxWorldSnapshot(
            self.spec.world_id, self.spec.kind, SandboxWorldState.ACTIVE, 0
        )


class _Provider:
    provider_id = "fake"

    def provision(self, spec, context):
        return _World()


def test_provider_returns_one_world_lifecycle_owner_and_cleanup_proves_quiescence():
    provider: SandboxProvider = _Provider()
    world: SandboxWorld = provider.provision(
        SandboxWorldSpec("container-1", SandboxWorldKind.CONTAINER), object()
    )
    assert world.snapshot().kind is SandboxWorldKind.CONTAINER
    assert world.cancel(reason="stop")
    result = world.cleanup(timeout=0)
    assert result.quiescent and result.active_resources == 0
