"""Fail-closed providers for container and remote execution worlds.

These providers are application contracts with deterministic reference
implementations.  They never imply that a local process is a security
boundary, and they require an explicitly configured adapter before work can
be submitted.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .world_control import (
    ExecutionSurface,
    ExecutionWorldKind,
    IsolationClaim,
    IsolationTruth,
    SharedExecutionWorld,
)


class WorldWorker(Protocol):
    def submit(self, *, world_id: str, payload: str) -> str: ...


@dataclass(frozen=True, slots=True)
class ContainerWorldConfig:
    world_id: str
    image: str
    allowed: bool = False

    def __post_init__(self) -> None:
        if not self.world_id.strip() or not self.image.strip():
            raise ValueError("container world identity and image are required")


@dataclass(frozen=True, slots=True)
class RemoteWorldConfig:
    world_id: str
    worker_id: str
    endpoint: str
    allowed: bool = False

    def __post_init__(self) -> None:
        if not all(value.strip() for value in (self.world_id, self.worker_id, self.endpoint)):
            raise ValueError("remote world identity and endpoint are required")
        endpoint = self.endpoint.strip()
        scheme, separator, authority = endpoint.partition("://")
        if scheme.lower() != "https" or separator == "" or not authority.strip():
            raise ValueError("remote worker endpoint must be an HTTPS URL")


class GuardedContainerWorld:
    """Default container world: describe freely, execute only when enabled."""

    def __init__(self, config: ContainerWorldConfig, worker: WorldWorker | None = None) -> None:
        self.config = config
        self.worker = worker
        self.world = SharedExecutionWorld(
            config.world_id,
            ExecutionWorldKind.CONTAINER,
            frozenset({ExecutionSurface.CODE, ExecutionSurface.FILESYSTEM, ExecutionSurface.SHELL}),
            IsolationClaim(IsolationTruth.UNVERIFIED, "container isolation requires adapter evidence"),
            provider_id=config.image,
        )

    def submit(self, payload: str) -> str:
        if not self.config.allowed or self.worker is None:
            raise PermissionError("container execution is not configured")
        return self.worker.submit(world_id=self.world.world_id, payload=payload)


class ConfiguredRemoteWorld:
    """Remote worker boundary with explicit identity and fail-closed routing."""

    def __init__(self, config: RemoteWorldConfig, worker: WorldWorker | None = None) -> None:
        self.config = config
        self.worker = worker
        self.world = SharedExecutionWorld(
            config.world_id,
            ExecutionWorldKind.REMOTE,
            frozenset({ExecutionSurface.CODE, ExecutionSurface.FILESYSTEM, ExecutionSurface.SHELL}),
            IsolationClaim(IsolationTruth.UNVERIFIED, "remote boundary requires transport and worker evidence"),
            provider_id=config.worker_id,
        )

    def submit(self, payload: str) -> str:
        if not self.config.allowed or self.worker is None:
            raise PermissionError("remote execution is not configured")
        return self.worker.submit(world_id=self.world.world_id, payload=payload)


__all__ = ["ConfiguredRemoteWorld", "ContainerWorldConfig", "GuardedContainerWorld", "RemoteWorldConfig", "WorldWorker"]
