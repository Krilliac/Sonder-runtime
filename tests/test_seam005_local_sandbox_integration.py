from __future__ import annotations

from pathlib import Path

import pytest

from sonder_runtime.adapters.sandbox import (
    LocalSandboxProvider,
    LocalSandboxUnavailable,
)
from sonder_runtime.adapters.execution import WorldUnavailable
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.execution.world_control import IsolationTruth
from sonder_runtime.application.ports import (
    SandboxPolicy,
    SandboxResourceLimits,
    SandboxWorldKind,
    SandboxWorldSpec,
)
from sonder_runtime.application.ports.execution_world import ShellRequest


def _context(root: Path):
    return local_owner_context(
        correlation_id="seam-005-local",
        workspace_roots=(root,),
    )


def test_local_provider_returns_bounded_workspace_capability_without_security_claim(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "small.txt").write_text("ok", encoding="utf-8")

    world = LocalSandboxProvider().provision(
        SandboxWorldSpec(
            "local-1",
            SandboxWorldKind.LOCAL,
            workspace=workspace,
            policy=SandboxPolicy(
                resource_limits=SandboxResourceLimits(
                    max_path_length=256,
                    max_file_bytes=16,
                    max_active_resources=1,
                )
            ),
        ),
        _context(tmp_path),
    )

    assert world.workspace == workspace.resolve()
    assert world.resolve_path("small.txt") == workspace.resolve() / "small.txt"
    assert world.isolation.truth is IsolationTruth.FAILURE_ISOLATION_ONLY
    assert not world.isolation.is_security_boundary
    with pytest.raises(LocalSandboxUnavailable, match="escapes"):
        world.resolve_path("../outside.txt")
    with pytest.raises(WorldUnavailable, match="not declared"):
        world.execution_world.shell.execute(ShellRequest("echo unsafe"), _context(tmp_path))


def test_local_provider_enforces_file_and_active_resource_limits(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "large.bin").write_bytes(b"12345")
    policy = SandboxPolicy(
        resource_limits=SandboxResourceLimits(max_file_bytes=4, max_active_resources=1)
    )
    world = LocalSandboxProvider().provision(
        SandboxWorldSpec("local-2", SandboxWorldKind.LOCAL, policy, workspace),
        _context(tmp_path),
    )

    with pytest.raises(LocalSandboxUnavailable, match="file exceeds"):
        world.resolve_path("large.bin")
    world.acquire_resource()
    with pytest.raises(LocalSandboxUnavailable, match="limit reached"):
        world.acquire_resource()
    incomplete = world.cleanup(timeout=0)
    assert not incomplete.quiescent and incomplete.active_resources == 1
    world.release_resource()
    complete = world.cleanup(timeout=0)
    assert complete.quiescent and complete.active_resources == 0
    with pytest.raises(LocalSandboxUnavailable, match="path access"):
        world.resolve_path("small.txt")


def test_read_only_world_is_supported_only_as_a_non_mutating_capability(tmp_path):
    workspace = tmp_path / "readonly"
    workspace.mkdir()
    world = LocalSandboxProvider().provision(
        SandboxWorldSpec(
            "read-only-1",
            SandboxWorldKind.READ_ONLY,
            SandboxPolicy(read_only=True, allow_write=False),
            workspace,
        ),
        _context(tmp_path),
    )
    assert world.spec.policy.read_only
    assert world.snapshot().state.value == "active"
    assert world.cancel(reason="stop")
    assert world.cleanup(timeout=0).quiescent


def test_local_provider_rejects_unsupported_kinds_missing_roots_and_bad_context(tmp_path):
    provider = LocalSandboxProvider()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with pytest.raises(LocalSandboxUnavailable, match="does not support"):
        provider.provision(
            SandboxWorldSpec("container", SandboxWorldKind.CONTAINER, workspace=workspace),
            _context(tmp_path),
        )
    with pytest.raises(LocalSandboxUnavailable, match="workspace is required"):
        provider.provision(
            SandboxWorldSpec("local", SandboxWorldKind.LOCAL), _context(tmp_path)
        )
    outside = tmp_path.parent / "outside-sandbox-seam005"
    outside.mkdir(exist_ok=True)
    try:
        with pytest.raises(LocalSandboxUnavailable, match="outside"):
            provider.provision(
                SandboxWorldSpec("outside", SandboxWorldKind.LOCAL, workspace=outside),
                _context(tmp_path),
            )
    finally:
        outside.rmdir()
