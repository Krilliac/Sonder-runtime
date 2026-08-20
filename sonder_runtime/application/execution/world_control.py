"""Typed execution-world and job-control contracts (REMAINING-EXEC-001-006).

This module is an application boundary.  It does not start processes, open
terminals, create containers, or claim that a provider is secure.  Adapters
bind the surfaces of one :class:`SharedExecutionWorld` and may implement the
protocols below with local, container, or remote machinery.

The distinction between failure isolation and a security boundary is
intentional.  A crashed child, a worktree, or a Python exception is not by
itself a security boundary; callers must carry the explicit truth label into
receipts and UI.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Callable, Iterable, Protocol

from ..ports.jobs import JobIdentity, JobStatus, TERMINAL_JOB_STATUSES


class ExecutionWorldKind(str, Enum):
    LOCAL = "local"
    CONTAINER = "container"
    REMOTE = "remote"


class ExecutionSurface(str, Enum):
    FILESYSTEM = "filesystem"
    SHELL = "shell"
    SUBPROCESS = "subprocess"
    TERMINAL = "terminal"
    LSP = "lsp"
    CODE = "code"


class IsolationTruth(str, Enum):
    """What an adapter is entitled to say about containment."""

    UNVERIFIED = "unverified"
    FAILURE_ISOLATION_ONLY = "failure_isolation_only"
    SECURITY_BOUNDARY_VERIFIED = "security_boundary_verified"


@dataclass(frozen=True)
class IsolationClaim:
    truth: IsolationTruth
    rationale: str
    evidence_ref: str | None = None

    def __post_init__(self) -> None:
        if not self.rationale.strip():
            raise ValueError("isolation rationale must be non-empty")
        if self.truth is IsolationTruth.SECURITY_BOUNDARY_VERIFIED and not (
            self.evidence_ref and self.evidence_ref.strip()
        ):
            raise ValueError(
                "verified security boundaries require an evidence reference"
            )

    @property
    def is_security_boundary(self) -> bool:
        return self.truth is IsolationTruth.SECURITY_BOUNDARY_VERIFIED


@dataclass(frozen=True)
class SharedExecutionWorld:
    """Stable authority shared by every execution surface."""

    world_id: str
    kind: ExecutionWorldKind
    surfaces: frozenset[ExecutionSurface]
    isolation: IsolationClaim
    provider_id: str = ""

    def __post_init__(self) -> None:
        if not self.world_id.strip():
            raise ValueError("world_id must be non-empty")
        if not self.surfaces:
            raise ValueError("a world must expose at least one execution surface")
        if not isinstance(self.isolation, IsolationClaim):
            raise TypeError("isolation must be an IsolationClaim")

    def bind(self, surface: ExecutionSurface) -> "WorldBinding":
        if surface not in self.surfaces:
            raise ValueError(f"surface {surface.value!r} is not enabled in this world")
        return WorldBinding(self.world_id, surface)


@dataclass(frozen=True)
class WorldBinding:
    world_id: str
    surface: ExecutionSurface

    def __post_init__(self) -> None:
        if not self.world_id.strip():
            raise ValueError("binding world_id must be non-empty")


def require_same_world(*bindings: WorldBinding) -> str:
    """Return the shared id or reject a cross-world operation."""

    if not bindings:
        raise ValueError("at least one world binding is required")
    ids = {binding.world_id for binding in bindings}
    if len(ids) != 1:
        raise ValueError("execution surfaces must belong to the same world")
    return bindings[0].world_id


class OutputStream(str, Enum):
    STDOUT = "stdout"
    STDERR = "stderr"
    TERMINAL = "terminal"


@dataclass(frozen=True, order=True)
class OutputWatermark:
    """Opaque, monotonic cursor for one output stream."""

    sequence: int = 0

    def __post_init__(self) -> None:
        if self.sequence < 0:
            raise ValueError("watermark sequence cannot be negative")


@dataclass(frozen=True)
class SpillReference:
    digest: str
    preview: str
    size: int
    mime_type: str
    owner_id: str

    def __post_init__(self) -> None:
        if not self.digest.strip() or not self.owner_id.strip():
            raise ValueError("spill digest and owner_id must be non-empty")
        if self.size < 0:
            raise ValueError("spill size cannot be negative")
        if not self.mime_type.strip():
            raise ValueError("spill mime_type must be non-empty")


@dataclass(frozen=True)
class OutputEvent:
    watermark: OutputWatermark
    stream: OutputStream
    data: str
    spill: SpillReference | None = None


@dataclass(frozen=True)
class OutputPage:
    events: tuple[OutputEvent, ...]
    next_watermark: OutputWatermark
    has_more: bool
    truncated: bool = False


class BoundedOutputBuffer:
    """Retain bounded output and expose non-repeating watermark reads.

    A producer may provide a spill reference when the full payload is stored
    elsewhere.  The buffer never fabricates a spill or claims that retained
    output is complete; callers can inspect ``truncated`` and ``has_more``.
    """

    def __init__(self, *, max_events: int = 256, max_bytes: int = 64 * 1024) -> None:
        if max_events < 1 or max_bytes < 1:
            raise ValueError("output bounds must be positive")
        self._max_events = max_events
        self._max_bytes = max_bytes
        self._events: deque[OutputEvent] = deque()
        self._bytes = 0
        self._next = 1
        self._dropped_before = 0

    def append(
        self,
        stream: OutputStream,
        data: str,
        *,
        spill: SpillReference | None = None,
    ) -> OutputEvent:
        if not isinstance(data, str):
            raise TypeError("output data must be text")
        event = OutputEvent(OutputWatermark(self._next), stream, data, spill)
        self._next += 1
        self._events.append(event)
        self._bytes += len(data.encode("utf-8"))
        while self._events and (
            len(self._events) > self._max_events or self._bytes > self._max_bytes
        ):
            removed = self._events.popleft()
            self._bytes -= len(removed.data.encode("utf-8"))
            self._dropped_before = removed.watermark.sequence
        return event

    def read(
        self,
        after: OutputWatermark | None = None,
        *,
        max_events: int = 64,
        max_bytes: int = 16 * 1024,
    ) -> OutputPage:
        if max_events < 1 or max_bytes < 1:
            raise ValueError("read bounds must be positive")
        cursor = after or OutputWatermark(0)
        candidates = tuple(
            event for event in self._events if event.watermark.sequence > cursor.sequence
        )
        selected: list[OutputEvent] = []
        used = 0
        for event in candidates:
            size = len(event.data.encode("utf-8"))
            if selected and (len(selected) >= max_events or used + size > max_bytes):
                break
            selected.append(event)
            used += size
            if len(selected) >= max_events or used >= max_bytes:
                break
        last = selected[-1].watermark if selected else cursor
        has_more = any(event.watermark.sequence > last.sequence for event in candidates)
        truncated = cursor.sequence < self._dropped_before
        return OutputPage(tuple(selected), last, has_more, truncated)


@dataclass(frozen=True)
class ExecutionJob:
    identity: JobIdentity
    world: WorldBinding
    status: JobStatus = JobStatus.PENDING
    revision: int = 0
    terminal_id: str | None = None
    output_watermark: OutputWatermark = OutputWatermark()

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_JOB_STATUSES


@dataclass(frozen=True)
class TerminalSession:
    terminal_id: str
    world: WorldBinding
    columns: int = 80
    rows: int = 24
    stopped: bool = False

    def __post_init__(self) -> None:
        if not self.terminal_id.strip():
            raise ValueError("terminal_id must be non-empty")
        if self.columns < 1 or self.rows < 1:
            raise ValueError("terminal dimensions must be positive")


class ExecutionWorldController(Protocol):
    """One typed control port for jobs and persistent terminals."""

    world: SharedExecutionWorld

    def start(self, identity: JobIdentity, *, surface: ExecutionSurface = ExecutionSurface.CODE) -> ExecutionJob: ...
    def list(self, *, include_terminal: bool = True, limit: int = 100) -> tuple[ExecutionJob, ...]: ...
    def poll(self, job_id: str) -> ExecutionJob: ...
    def stream(self, job_id: str, *, after: OutputWatermark | None = None, max_events: int = 64, max_bytes: int = 16 * 1024) -> OutputPage: ...
    def cancel(self, job_id: str, *, reason: str = "cancelled") -> ExecutionJob: ...
    def collect(self, job_id: str) -> ExecutionJob: ...
    def open_terminal(self, terminal_id: str, *, columns: int = 80, rows: int = 24) -> TerminalSession: ...
    def reconnect_terminal(self, terminal_id: str) -> TerminalSession: ...
    def send_terminal(self, terminal_id: str, data: str) -> OutputEvent: ...
    def resize_terminal(self, terminal_id: str, *, columns: int, rows: int) -> TerminalSession: ...
    def stop_terminal(self, terminal_id: str) -> TerminalSession: ...


class InMemoryExecutionWorldController:
    """Deterministic reference adapter for contract and integration tests."""

    def __init__(self, world: SharedExecutionWorld, *, output: BoundedOutputBuffer | None = None) -> None:
        self.world = world
        self._output = output or BoundedOutputBuffer()
        self._jobs: dict[str, ExecutionJob] = {}
        self._terminals: dict[str, TerminalSession] = {}

    def _world_binding(self, surface: ExecutionSurface) -> WorldBinding:
        return self.world.bind(surface)

    def start(self, identity: JobIdentity, *, surface: ExecutionSurface = ExecutionSurface.CODE) -> ExecutionJob:
        if identity.job_id in self._jobs:
            raise ValueError("job already exists")
        job = ExecutionJob(identity, self._world_binding(surface), JobStatus.RUNNING, 1)
        self._jobs[identity.job_id] = job
        return job

    def list(self, *, include_terminal: bool = True, limit: int = 100) -> tuple[ExecutionJob, ...]:
        if limit < 1:
            raise ValueError("limit must be positive")
        values = tuple(self._jobs.values())
        if not include_terminal:
            values = tuple(job for job in values if not job.is_terminal)
        return values[:limit]

    def poll(self, job_id: str) -> ExecutionJob:
        try:
            return self._jobs[job_id]
        except KeyError as exc:
            raise KeyError(f"unknown job {job_id!r}") from exc

    def stream(self, job_id: str, *, after: OutputWatermark | None = None, max_events: int = 64, max_bytes: int = 16 * 1024) -> OutputPage:
        self.poll(job_id)
        return self._output.read(after, max_events=max_events, max_bytes=max_bytes)

    def cancel(self, job_id: str, *, reason: str = "cancelled") -> ExecutionJob:
        job = self.poll(job_id)
        if job.is_terminal:
            return job
        updated = replace(job, status=JobStatus.CANCELLED, revision=job.revision + 1)
        self._jobs[job_id] = updated
        return updated

    def collect(self, job_id: str) -> ExecutionJob:
        job = self.poll(job_id)
        if not job.is_terminal:
            raise ValueError("job is not terminal")
        return job

    def open_terminal(self, terminal_id: str, *, columns: int = 80, rows: int = 24) -> TerminalSession:
        if terminal_id in self._terminals and not self._terminals[terminal_id].stopped:
            raise ValueError("terminal already exists")
        session = TerminalSession(terminal_id, self._world_binding(ExecutionSurface.TERMINAL), columns, rows)
        self._terminals[terminal_id] = session
        return session

    def reconnect_terminal(self, terminal_id: str) -> TerminalSession:
        try:
            session = self._terminals[terminal_id]
        except KeyError as exc:
            raise KeyError(f"unknown terminal {terminal_id!r}") from exc
        if session.stopped:
            raise ValueError("terminal is stopped")
        return session

    def send_terminal(self, terminal_id: str, data: str) -> OutputEvent:
        self.reconnect_terminal(terminal_id)
        return self._output.append(OutputStream.TERMINAL, data)

    def resize_terminal(self, terminal_id: str, *, columns: int, rows: int) -> TerminalSession:
        session = self.reconnect_terminal(terminal_id)
        updated = replace(session, columns=columns, rows=rows)
        self._terminals[terminal_id] = updated
        return updated

    def stop_terminal(self, terminal_id: str) -> TerminalSession:
        session = self.reconnect_terminal(terminal_id)
        updated = replace(session, stopped=True)
        self._terminals[terminal_id] = updated
        return updated


__all__ = [
    "BoundedOutputBuffer",
    "ExecutionJob",
    "ExecutionSurface",
    "ExecutionWorldController",
    "ExecutionWorldKind",
    "InMemoryExecutionWorldController",
    "IsolationClaim",
    "IsolationTruth",
    "OutputEvent",
    "OutputPage",
    "OutputStream",
    "OutputWatermark",
    "SharedExecutionWorld",
    "SpillReference",
    "TerminalSession",
    "WorldBinding",
    "require_same_world",
]
