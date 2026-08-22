"""Bounded local sandbox capability boundary.

This adapter deliberately does not execute code, spawn processes, or provide
OS-level isolation.  It provides only a workspace-contained path capability
and a lifecycle owner whose execution surfaces fail closed.  The resulting
isolation label is therefore failure isolation only, never a security claim.
"""
from __future__ import annotations

from pathlib import Path
from threading import Lock

from ..application.context import OperationContext
from ..application.execution.world_control import IsolationClaim, IsolationTruth
from ..application.ports.sandbox import (
    SandboxCleanupResult,
    SandboxProvider,
    SandboxResourceLimits,
    SandboxWorld,
    SandboxWorldKind,
    SandboxWorldSnapshot,
    SandboxWorldSpec,
    SandboxWorldState,
)
from .execution.worlds import ReferenceExecutionWorld, WorldIdentity, WorldUnavailable


class LocalSandboxUnavailable(RuntimeError):
    """Raised when a local capability cannot be proven within its bounds."""


def _require_context(context: OperationContext) -> None:
    if context.cancellation.cancelled or context.expired:
        raise LocalSandboxUnavailable("operation context is cancelled or expired")


def _contained(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


class LocalSandboxWorld(SandboxWorld):
    """One local workspace capability with explicit resource accounting."""

    def __init__(self, spec: SandboxWorldSpec, *, provider_id: str) -> None:
        if spec.workspace is None:
            raise ValueError("local sandbox workspace is required")
        self._spec = spec
        self.provider_id = provider_id
        self.workspace = spec.workspace
        self.limits: SandboxResourceLimits = spec.policy.resource_limits
        self._execution_world = ReferenceExecutionWorld(
            WorldIdentity(spec.world_id, provider_id, spec.kind), frozenset()
        )
        self._lock = Lock()
        self._active_resources = 0
        self._state = SandboxWorldState.ACTIVE
        self._reason: str | None = None

    @property
    def spec(self) -> SandboxWorldSpec:
        return self._spec

    @property
    def execution_world(self):
        return self._execution_world

    @property
    def isolation(self) -> IsolationClaim:
        return IsolationClaim(
            IsolationTruth.FAILURE_ISOLATION_ONLY,
            "local workspace path capability is bounded; OS isolation is unverified",
        )

    def resolve_path(self, path: str | Path) -> Path:
        """Resolve an existing or prospective path without escaping workspace."""

        with self._lock:
            if self._state is not SandboxWorldState.ACTIVE:
                raise LocalSandboxUnavailable(
                    f"local sandbox is {self._state.value}; path access is unavailable"
                )
        raw = Path(path)
        candidate = (raw if raw.is_absolute() else self.workspace / raw).resolve()
        if len(str(candidate)) > self.limits.max_path_length:
            raise LocalSandboxUnavailable("path exceeds the sandbox path limit")
        if not _contained(self.workspace, candidate):
            raise LocalSandboxUnavailable("path escapes the sandbox workspace")
        if candidate.exists() and candidate.is_file():
            try:
                size = candidate.stat().st_size
            except OSError as exc:
                raise LocalSandboxUnavailable("path metadata is unavailable") from exc
            if size > self.limits.max_file_bytes:
                raise LocalSandboxUnavailable("file exceeds the sandbox resource limit")
        return candidate

    def acquire_resource(self) -> None:
        with self._lock:
            if self._state is not SandboxWorldState.ACTIVE:
                raise LocalSandboxUnavailable("sandbox is not accepting resources")
            if self._active_resources >= self.limits.max_active_resources:
                raise LocalSandboxUnavailable("active resource limit reached")
            self._active_resources += 1

    def release_resource(self) -> None:
        with self._lock:
            if self._active_resources:
                self._active_resources -= 1

    def cancel(self, *, reason: str = "cancellation requested") -> bool:
        with self._lock:
            if self._state in (SandboxWorldState.QUIESCENT, SandboxWorldState.CLOSED):
                return False
            first = self._state is SandboxWorldState.ACTIVE
            if first:
                self._state = SandboxWorldState.CANCELLATION_REQUESTED
                self._reason = reason
        self._execution_world.cancel(reason=reason)
        return first

    def cleanup(self, timeout: float | None = None) -> SandboxCleanupResult:
        with self._lock:
            active = self._active_resources
            if active:
                self._state = SandboxWorldState.CANCELLATION_REQUESTED
                return SandboxCleanupResult(False, active, self._state)
            self._state = SandboxWorldState.QUIESCENT
        self._execution_world.cleanup(timeout)
        return SandboxCleanupResult(True, 0, SandboxWorldState.QUIESCENT)

    def snapshot(self) -> SandboxWorldSnapshot:
        with self._lock:
            return SandboxWorldSnapshot(
                self.spec.world_id,
                self.spec.kind,
                self._state,
                self._active_resources,
                self._reason,
            )


class LocalSandboxProvider(SandboxProvider):
    """Provide only bounded local/read-only workspace capabilities."""

    provider_id = "local-capability-boundary"

    def provision(self, spec: SandboxWorldSpec, context: OperationContext) -> LocalSandboxWorld:
        _require_context(context)
        if spec.kind not in (SandboxWorldKind.LOCAL, SandboxWorldKind.READ_ONLY):
            raise LocalSandboxUnavailable("local provider does not support this world kind")
        if spec.workspace is None:
            raise LocalSandboxUnavailable("a workspace is required for local worlds")
        workspace = spec.workspace.expanduser().resolve()
        if not workspace.is_dir():
            raise LocalSandboxUnavailable("workspace must be an existing directory")
        if context.workspace_roots and not any(
            _contained(root.expanduser().resolve(), workspace)
            for root in context.workspace_roots
        ):
            raise LocalSandboxUnavailable("workspace is outside the operation roots")
        effective = SandboxWorldSpec(
            spec.world_id,
            spec.kind,
            spec.policy,
            workspace,
            spec.image,
            spec.endpoint,
        )
        return LocalSandboxWorld(effective, provider_id=self.provider_id)


__all__ = ["LocalSandboxProvider", "LocalSandboxUnavailable", "LocalSandboxWorld"]
