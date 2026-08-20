"""Guarded reference execution-world providers.

These adapters establish the execution-world boundary without pretending that
an unavailable container engine or remote worker is usable.  They are useful
for composition, policy, and contract tests; concrete process transports can
replace the fail-closed service capabilities later without changing identity
or lifecycle semantics.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from threading import Lock
from typing import Callable, FrozenSet
from urllib.parse import urlparse

from ...application.context import OperationContext
from ...application.ports.execution_world import (
    CleanupResult,
    ExecutionResult,
    ExecutionWorld,
    ExecutionWorldSnapshot,
    ExecutionWorldSpec,
    ExecutionWorldState,
    ShellRequest,
    SubprocessHandle,
    SubprocessRequest,
    SubprocessRuntime,
    TerminalRequest,
    TerminalService,
)
from ...application.ports.sandbox import (
    SandboxCleanupResult,
    SandboxProvider,
    SandboxWorld,
    SandboxWorldKind,
    SandboxWorldSnapshot,
    SandboxWorldSpec,
    SandboxWorldState,
)


class WorldCapability(StrEnum):
    FILESYSTEM = "filesystem"
    SHELL = "shell"
    SUBPROCESS = "subprocess"
    TERMINAL = "terminal"
    LSP = "lsp"
    CODE = "code"


class WorldUnavailable(RuntimeError):
    """Raised when a world cannot prove that an execution operation is safe."""


@dataclass(frozen=True)
class WorldIdentity:
    """Stable identity carried by every provider-created world."""

    world_id: str
    provider_id: str
    kind: SandboxWorldKind
    worker_id: str | None = None
    endpoint: str | None = None

    def __post_init__(self) -> None:
        if not self.world_id.strip() or not self.provider_id.strip():
            raise ValueError("world and provider identity must be non-empty")
        if self.kind is SandboxWorldKind.REMOTE and not self.worker_id:
            raise ValueError("remote worlds require a worker identity")
        if self.kind is not SandboxWorldKind.REMOTE and self.endpoint is not None:
            raise ValueError("only remote worlds may carry an endpoint")


def _require_context(context: OperationContext) -> None:
    if context.cancellation.cancelled or context.expired:
        raise WorldUnavailable("operation context is cancelled or expired")


class _FailClosedSubprocesses(SubprocessRuntime):
    def __init__(self, reason: str) -> None:
        self._reason = reason

    def start(self, request: SubprocessRequest, context: OperationContext) -> SubprocessHandle:
        _require_context(context)
        raise WorldUnavailable(self._reason)


class _FailClosedShell:
    def __init__(self, reason: str) -> None:
        self._reason = reason

    def execute(self, request: ShellRequest, context: OperationContext) -> ExecutionResult:
        _require_context(context)
        raise WorldUnavailable(self._reason)


class _FailClosedTerminals(TerminalService):
    def __init__(self, reason: str) -> None:
        self._reason = reason

    def open(self, request: TerminalRequest, context: OperationContext):
        _require_context(context)
        raise WorldUnavailable(self._reason)


class ReferenceExecutionWorld(ExecutionWorld):
    """Lifecycle-only execution world whose operational surfaces fail closed."""

    def __init__(self, identity: WorldIdentity, capabilities: FrozenSet[WorldCapability]) -> None:
        self.identity = identity
        self.capabilities = capabilities
        self._spec = ExecutionWorldSpec(identity.world_id)
        reason = f"{identity.provider_id} has no verified execution transport"
        self._subprocesses = _FailClosedSubprocesses(reason)
        self._shell = _FailClosedShell(reason)
        self._terminals = _FailClosedTerminals(reason)
        self._lock = Lock()
        self._state = ExecutionWorldState.ACTIVE
        self._reason: str | None = None

    @property
    def spec(self) -> ExecutionWorldSpec:
        return self._spec

    @property
    def subprocesses(self) -> SubprocessRuntime:
        return self._subprocesses

    @property
    def shell(self):
        return self._shell

    @property
    def terminals(self) -> TerminalService:
        return self._terminals

    def cancel(self, *, reason: str = "cancellation requested") -> bool:
        with self._lock:
            if self._state in (ExecutionWorldState.QUIESCENT, ExecutionWorldState.CLOSED):
                return False
            first = self._state is ExecutionWorldState.ACTIVE
            if first:
                self._state = ExecutionWorldState.CANCELLATION_REQUESTED
                self._reason = reason
            return first

    def cleanup(self, timeout: float | None = None) -> CleanupResult:
        with self._lock:
            self._state = ExecutionWorldState.QUIESCENT
            return CleanupResult(True, 0, self._state)

    def snapshot(self) -> ExecutionWorldSnapshot:
        with self._lock:
            return ExecutionWorldSnapshot(self.spec.world_id, self._state, 0, self._reason)


class ReferenceSandboxWorld(SandboxWorld):
    def __init__(self, spec: SandboxWorldSpec, identity: WorldIdentity, capabilities: FrozenSet[WorldCapability]) -> None:
        self._spec = spec
        self.identity = identity
        self.capabilities = capabilities
        self._execution_world = ReferenceExecutionWorld(identity, capabilities)

    @property
    def spec(self) -> SandboxWorldSpec:
        return self._spec

    @property
    def execution_world(self) -> ExecutionWorld:
        return self._execution_world

    def cancel(self, *, reason: str = "cancellation requested") -> bool:
        return self.execution_world.cancel(reason=reason)

    def cleanup(self, timeout: float | None = None) -> SandboxCleanupResult:
        result = self.execution_world.cleanup(timeout)
        return SandboxCleanupResult(result.quiescent, result.active_resources, SandboxWorldState.QUIESCENT)

    def snapshot(self) -> SandboxWorldSnapshot:
        state = self.execution_world.snapshot()
        return SandboxWorldSnapshot(
            self.spec.world_id,
            self.spec.kind,
            SandboxWorldState(state.state.value),
            state.active_resources,
            state.cancellation_reason,
        )


_DEFAULT_CAPABILITIES = frozenset(WorldCapability)


class GuardedContainerProvider(SandboxProvider):
    """Default provider: container only, with no host-execution fallback."""

    provider_id = "container-default"

    def __init__(self, *, image: str) -> None:
        if not image.strip():
            raise ValueError("a guarded container image is required")
        self.image = image

    def provision(self, spec: SandboxWorldSpec, context: OperationContext) -> SandboxWorld:
        _require_context(context)
        if spec.kind is not SandboxWorldKind.CONTAINER:
            raise WorldUnavailable("default execution provider accepts container worlds only")
        if spec.image and spec.image != self.image:
            raise WorldUnavailable("requested container image is not configured")
        effective = SandboxWorldSpec(
            spec.world_id, spec.kind, spec.policy, spec.workspace, self.image, None
        )
        identity = WorldIdentity(spec.world_id, self.provider_id, spec.kind)
        return ReferenceSandboxWorld(effective, identity, _DEFAULT_CAPABILITIES)


class ConfiguredRemoteWorkerProvider(SandboxProvider):
    """Reference remote boundary; it never silently becomes a local worker."""

    provider_id = "remote-worker"

    def __init__(self, *, endpoint: str, worker_id: str, capabilities: frozenset[WorldCapability]) -> None:
        parsed = urlparse(endpoint)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("remote worker endpoint must be an HTTPS URL")
        if not worker_id.strip():
            raise ValueError("remote worker identity must be non-empty")
        if not capabilities:
            raise ValueError("remote worker must declare capabilities")
        self.endpoint = endpoint
        self.worker_id = worker_id
        self.capabilities = frozenset(capabilities)

    def provision(self, spec: SandboxWorldSpec, context: OperationContext) -> SandboxWorld:
        _require_context(context)
        if spec.kind is not SandboxWorldKind.REMOTE:
            raise WorldUnavailable("remote provider accepts remote worlds only")
        if spec.endpoint != self.endpoint:
            raise WorldUnavailable("remote endpoint is not the configured endpoint")
        identity = WorldIdentity(
            spec.world_id, self.provider_id, spec.kind, self.worker_id, self.endpoint
        )
        return ReferenceSandboxWorld(spec, identity, self.capabilities)


def default_container_provider(*, image: str) -> GuardedContainerProvider:
    """Construct the guarded default explicitly; absent configuration fails."""

    return GuardedContainerProvider(image=image)


__all__ = [
    "ConfiguredRemoteWorkerProvider",
    "GuardedContainerProvider",
    "ReferenceExecutionWorld",
    "ReferenceSandboxWorld",
    "WorldCapability",
    "WorldIdentity",
    "WorldUnavailable",
    "default_container_provider",
]
