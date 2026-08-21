"""WP3-SEAM-005 provider-neutral sandbox world contract.

The application owns the requested world kind and policy.  A provider owns
the concrete isolation mechanism and returns one lifecycle owner whose
execution-world capability is used by filesystem, process, shell, and
terminal adapters.  This module performs no isolation and never starts a
process or contacts a remote service.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from ..context import OperationContext
from .execution_world import ExecutionWorld


class SandboxWorldKind(StrEnum):
    LOCAL = "local"
    CONTAINER = "container"
    REMOTE = "remote"
    READ_ONLY = "read_only"


@dataclass(frozen=True)
class SandboxResourceLimits:
    """Finite bounds a provider must enforce for one world request."""

    max_path_length: int = 4096
    max_file_bytes: int = 16 * 1024 * 1024
    max_active_resources: int = 32

    def __post_init__(self) -> None:
        if self.max_path_length < 1:
            raise ValueError("max_path_length must be positive")
        if self.max_file_bytes < 1:
            raise ValueError("max_file_bytes must be positive")
        if self.max_active_resources < 1:
            raise ValueError("max_active_resources must be positive")


@dataclass(frozen=True)
class SandboxPolicy:
    """Authority requested for one world.

    These are constraints, not grants.  A provider may apply stricter
    limits, but must not widen them.  ``read_only`` is an explicit policy
    invariant: a read-only world cannot permit writes or persistent changes.
    """

    read_only: bool = False
    allow_write: bool = True
    allow_network: bool = False
    allow_process: bool = True
    egress_hosts: tuple[str, ...] = ()
    persistent_changes: bool = False
    resource_limits: SandboxResourceLimits = field(default_factory=SandboxResourceLimits)

    def __post_init__(self) -> None:
        if self.read_only and (self.allow_write or self.persistent_changes):
            raise ValueError("read-only policy cannot allow writes or persistence")
        if any(
            not isinstance(host, str) or not host.strip()
            for host in self.egress_hosts
        ):
            raise ValueError("egress_hosts must contain non-empty strings")
        if self.egress_hosts and not self.allow_network:
            raise ValueError("egress_hosts require allow_network=True")
        if not isinstance(self.resource_limits, SandboxResourceLimits):
            raise TypeError("resource_limits must be SandboxResourceLimits")


@dataclass(frozen=True)
class SandboxWorldSpec:
    """Stable request and policy inputs for one sandbox world."""

    world_id: str
    kind: SandboxWorldKind
    policy: SandboxPolicy = field(default_factory=SandboxPolicy)
    workspace: Path | None = None
    image: str | None = None
    endpoint: str | None = None

    def __post_init__(self) -> None:
        if not self.world_id.strip():
            raise ValueError("world_id must be non-empty")
        if not isinstance(self.kind, SandboxWorldKind):
            raise TypeError("kind must be a SandboxWorldKind")
        if self.kind is SandboxWorldKind.READ_ONLY and not self.policy.read_only:
            raise ValueError("read-only world requires a read-only policy")


class SandboxWorldState(StrEnum):
    ACTIVE = "active"
    CANCELLATION_REQUESTED = "cancellation_requested"
    QUIESCENT = "quiescent"
    CLOSED = "closed"


@dataclass(frozen=True)
class SandboxWorldSnapshot:
    world_id: str
    kind: SandboxWorldKind
    state: SandboxWorldState
    active_resources: int
    cancellation_reason: str | None = None


@dataclass(frozen=True)
class SandboxCleanupResult:
    """Cleanup evidence; incomplete cleanup must never be reported as safe."""

    quiescent: bool
    active_resources: int
    state: SandboxWorldState


class SandboxWorld(Protocol):
    """Lifecycle owner for an isolated execution world.

    ``cancel`` only requests shutdown.  ``cleanup`` is the barrier that
    rejects new work, propagates cancellation, releases provider resources,
    and waits for quiescence up to its timeout.  It is idempotent and may be
    called again after an incomplete bounded attempt.
    """

    @property
    def spec(self) -> SandboxWorldSpec: ...

    @property
    def execution_world(self) -> ExecutionWorld: ...

    # [any thread, thread-safe] First reason wins; does not prove quiescence.
    def cancel(self, *, reason: str = "cancellation requested") -> bool: ...

    # [any thread, thread-safe] False means resources remain active.
    def cleanup(self, timeout: float | None = None) -> SandboxCleanupResult: ...

    # [any thread, thread-safe] Snapshot only; no lifecycle transition.
    def snapshot(self) -> SandboxWorldSnapshot: ...


class SandboxProvider(Protocol):
    """Application port implemented by local/container/remote providers."""

    provider_id: str

    # [any thread, async safe] Must honor policy and operation cancellation.
    def provision(
        self, spec: SandboxWorldSpec, context: OperationContext
    ) -> SandboxWorld: ...


__all__ = [
    "SandboxCleanupResult",
    "SandboxPolicy",
    "SandboxResourceLimits",
    "SandboxProvider",
    "SandboxWorld",
    "SandboxWorldKind",
    "SandboxWorldSnapshot",
    "SandboxWorldSpec",
    "SandboxWorldState",
]
