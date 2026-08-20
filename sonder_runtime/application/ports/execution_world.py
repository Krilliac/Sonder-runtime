"""Application ports for the shared WP3 execution world (SEAM-004).

This module is deliberately infrastructure-free.  An adapter supplies the
process, shell, and terminal implementations; all three receive resources
from the same :class:`ExecutionWorld` owner.  Handles are capabilities, not
owners: closing a handle releases one resource, while world shutdown owns the
last-resort cleanup of every resource in the world.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol

from ..context import OperationContext


class ExecutionWorldState(str, Enum):
    ACTIVE = "active"
    CANCELLATION_REQUESTED = "cancellation_requested"
    QUIESCENT = "quiescent"
    CLOSED = "closed"


@dataclass(frozen=True)
class ExecutionWorldSpec:
    """Stable identity and policy inputs for one execution world."""

    world_id: str
    workspace: Path | None = None
    environment: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class SubprocessRequest:
    argv: tuple[str, ...]
    cwd: Path | None = None
    environment: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class ShellRequest:
    command: str
    cwd: Path | None = None
    environment: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class TerminalRequest:
    argv: tuple[str, ...]
    cwd: Path | None = None
    environment: tuple[tuple[str, str], ...] = ()
    columns: int = 80
    rows: int = 24


@dataclass(frozen=True)
class ExecutionResult:
    """Bounded result shared by one-shot shell and process operations."""

    exit_code: int | None
    stdout: str = ""
    stderr: str = ""
    cancelled: bool = False


@dataclass(frozen=True)
class TerminalChunk:
    stream: str
    data: str
    sequence: int


@dataclass(frozen=True)
class ExecutionWorldSnapshot:
    world_id: str
    state: ExecutionWorldState
    active_resources: int
    cancellation_reason: str | None = None


@dataclass(frozen=True)
class CleanupResult:
    quiescent: bool
    active_resources: int
    state: ExecutionWorldState


class ExecutionHandle(Protocol):
    """Non-owning capability for a resource owned by an execution world."""

    @property
    def resource_id(self) -> str: ...

    @property
    def world_id(self) -> str: ...

    # [any thread, thread-safe] Idempotent; does not close the world.
    def cancel(self, *, reason: str = "cancellation requested") -> None: ...

    # [any thread, thread-safe] Idempotent release of this resource.
    def close(self) -> None: ...


class SubprocessHandle(ExecutionHandle, Protocol):
    """A process handle whose process remains owned by its world."""

    # [any thread, thread-safe] Waits for this process only.
    def wait(self, timeout: float | None = None) -> ExecutionResult: ...


class TerminalHandle(ExecutionHandle, Protocol):
    """A terminal capability; terminal I/O is scoped to its world."""

    # [any thread, thread-safe] Writes input to the terminal.
    def send(self, data: str) -> None: ...

    # [any thread, thread-safe] Returns available output without owning it.
    def read(self, *, max_chunks: int = 64) -> tuple[TerminalChunk, ...]: ...

    # [any thread, thread-safe] Changes terminal dimensions.
    def resize(self, *, columns: int, rows: int) -> None: ...


class SubprocessRuntime(Protocol):
    """Spawn and supervise subprocesses inside one shared execution world."""

    # [any thread, async safe] The world rejects new work after cancellation.
    def start(
        self, request: SubprocessRequest, context: OperationContext
    ) -> SubprocessHandle: ...


class ShellExecutor(Protocol):
    """Execute one-shot shell work in the same world as subprocesses."""

    # [any thread, async safe] Cancellation terminates the owned shell work.
    def execute(
        self, request: ShellRequest, context: OperationContext
    ) -> ExecutionResult: ...


class TerminalService(Protocol):
    """Create persistent terminal sessions owned by the shared world."""

    # [any thread, async safe] The returned handle is non-owning.
    def open(
        self, request: TerminalRequest, context: OperationContext
    ) -> TerminalHandle: ...


class ExecutionWorld(Protocol):
    """Single owner and lifecycle boundary for subprocess, shell, and terminal.

    ``cancel`` is a request and may return before workers have exited.
    ``cleanup`` is the only shutdown operation: it rejects new work, propagates
    cancellation to every child, releases adapter resources, and waits for
    quiescence up to the supplied deadline.  A successful result is the proof
    that no child resource remains active; ``close`` must not be inferred from
    cancellation alone.
    """

    @property
    def spec(self) -> ExecutionWorldSpec: ...

    @property
    def subprocesses(self) -> SubprocessRuntime: ...

    @property
    def shell(self) -> ShellExecutor: ...

    @property
    def terminals(self) -> TerminalService: ...

    # [any thread, thread-safe] First reason wins; operation is idempotent.
    def cancel(self, *, reason: str = "cancellation requested") -> bool: ...

    # [any thread, thread-safe] Idempotent; false means cleanup is incomplete.
    def cleanup(self, timeout: float | None = None) -> CleanupResult: ...

    # [any thread, thread-safe] Snapshot only; does not change lifecycle.
    def snapshot(self) -> ExecutionWorldSnapshot: ...


__all__ = [
    "CleanupResult",
    "ExecutionHandle",
    "ExecutionResult",
    "ExecutionWorld",
    "ExecutionWorldSnapshot",
    "ExecutionWorldSpec",
    "ExecutionWorldState",
    "ShellExecutor",
    "ShellRequest",
    "SubprocessHandle",
    "SubprocessRequest",
    "SubprocessRuntime",
    "TerminalChunk",
    "TerminalHandle",
    "TerminalRequest",
    "TerminalService",
]
