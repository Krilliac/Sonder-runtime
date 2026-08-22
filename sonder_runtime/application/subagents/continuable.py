"""Durable, continuable child-agent execution (WP5 SUBAGENT-001).

The service owns lifecycle and concurrency rules; a repository owns durability.
The in-memory repository is deliberately a small test/reference adapter, not a
claim that process memory is durable.  A production adapter can implement the
same repository protocol with SQLite or another transactional store.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from threading import Event, Lock, Thread
from typing import Any, Protocol
from uuid import uuid4

from ..context import OperationContext
from ..ports.subagents import (
    InvalidSubagentRequest, SubagentBudget, SubagentError, SubagentHandle,
    SubagentRequest, SubagentResult, SubagentSnapshot, SubagentStatus,
    SubagentUsage, TERMINAL_SUBAGENT_STATUSES,
)


@dataclass(frozen=True)
class ContinuableCheckpoint:
    """An immutable, monotonic child state snapshot."""

    child_id: str
    sequence: int
    state: Mapping[str, Any] = field(default_factory=dict)
    cursor: str | None = None

    def __post_init__(self) -> None:
        if not self.child_id.strip() or self.sequence < 0:
            raise InvalidSubagentRequest("checkpoint child_id and non-negative sequence are required")
        if not isinstance(self.state, Mapping):
            raise InvalidSubagentRequest("checkpoint state must be a mapping")
        object.__setattr__(self, "state", deepcopy(dict(self.state)))


@dataclass(frozen=True)
class ContinuableRecord:
    """Durable metadata and the latest checkpoint for one child."""

    request: SubagentRequest
    status: SubagentStatus = SubagentStatus.CREATED
    checkpoint: ContinuableCheckpoint | None = None
    usage: SubagentUsage = SubagentUsage()
    result: SubagentResult | None = None
    recovery_required: bool = False
    cancellation_reason: str | None = None


class ContinuableSubagentRepository(Protocol):
    """Persistence port; writes must be atomic compare-and-set operations."""

    def create(self, record: ContinuableRecord) -> ContinuableRecord: ...
    def get(self, child_id: str) -> ContinuableRecord | None: ...
    def save_checkpoint(self, checkpoint: ContinuableCheckpoint, *, expected_sequence: int) -> ContinuableCheckpoint | None: ...
    def update(self, child_id: str, *, status: SubagentStatus, usage: SubagentUsage | None = None,
               result: SubagentResult | None = None, recovery_required: bool | None = None,
               cancellation_reason: str | None = None) -> ContinuableRecord | None: ...
    def list_recoverable(self) -> tuple[ContinuableRecord, ...]: ...


class InMemoryContinuableSubagentRepository:
    """Thread-safe reference adapter used by focused tests and local callers."""

    def __init__(self) -> None:
        self._items: dict[str, ContinuableRecord] = {}
        self._lock = Lock()

    def create(self, record: ContinuableRecord) -> ContinuableRecord:
        with self._lock:
            if record.request.child_id in self._items:
                raise InvalidSubagentRequest("child_id already exists")
            self._items[record.request.child_id] = record
            return record

    def get(self, child_id: str) -> ContinuableRecord | None:
        with self._lock:
            return self._items.get(child_id)

    def save_checkpoint(self, checkpoint: ContinuableCheckpoint, *, expected_sequence: int) -> ContinuableCheckpoint | None:
        with self._lock:
            current = self._items.get(checkpoint.child_id)
            if current is None:
                return None
            current_sequence = current.checkpoint.sequence if current.checkpoint else -1
            if current_sequence != expected_sequence or checkpoint.sequence != expected_sequence + 1:
                return None
            updated = ContinuableRecord(current.request, current.status, checkpoint, current.usage,
                                        current.result, current.recovery_required, current.cancellation_reason)
            self._items[checkpoint.child_id] = updated
            return checkpoint

    def update(self, child_id: str, *, status: SubagentStatus, usage: SubagentUsage | None = None,
               result: SubagentResult | None = None, recovery_required: bool | None = None,
               cancellation_reason: str | None = None) -> ContinuableRecord | None:
        with self._lock:
            current = self._items.get(child_id)
            if current is None:
                return None
            updated = ContinuableRecord(current.request, status, current.checkpoint,
                                        usage or current.usage, result,
                                        current.recovery_required if recovery_required is None else recovery_required,
                                        cancellation_reason if cancellation_reason is not None else current.cancellation_reason)
            self._items[child_id] = updated
            return updated

    def list_recoverable(self) -> tuple[ContinuableRecord, ...]:
        with self._lock:
            return tuple(item for item in self._items.values() if item.recovery_required)


class _Cancellation:
    def __init__(self) -> None:
        self._event = Event()
        self.reason = "cancellation requested"

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self, reason: str) -> bool:
        if self._event.is_set():
            return False
        self.reason = reason
        self._event.set()
        return True

    def wait(self, timeout: float | None = None) -> bool:
        return self._event.wait(timeout)


CheckpointWriter = Callable[[Mapping[str, Any], str | None], ContinuableCheckpoint]
Runner = Callable[[Mapping[str, Any], CheckpointWriter, _Cancellation], str]

# Compatibility names used by the package facade while the WP5 slices are
# being integrated.  They intentionally preserve the same immutable/store
# semantics instead of introducing a second state model.
ContinuableSubagentState = ContinuableRecord
SubagentCheckpoint = ContinuableCheckpoint
SubagentStore = InMemoryContinuableSubagentRepository


@dataclass(frozen=True)
class ResumeToken:
    child_id: str
    sequence: int


class ContinuableSubagentService:
    """Provider-compatible service with durable checkpoints and explicit resume."""

    def __init__(self, repository: ContinuableSubagentRepository) -> None:
        self._repository = repository
        self._controls: dict[str, _Cancellation] = {}
        self._threads: dict[str, Thread] = {}
        self._lock = Lock()

    def spawn(self, request: SubagentRequest, context: OperationContext, runner: Runner) -> SubagentHandle:
        child_id = request.child_id or f"child-{uuid4().hex}"
        request = SubagentRequest(request.parent_id, request.prompt, request.budget, child_id, request.metadata)
        record = self._repository.create(ContinuableRecord(request))
        control = _Cancellation()
        with self._lock:
            self._controls[child_id] = control
        self._launch(record, context, runner, control)
        return _Handle(self, child_id, request.parent_id)

    def _launch(self, record: ContinuableRecord, context: OperationContext, runner: Runner, control: _Cancellation) -> None:
        self._repository.update(record.request.child_id, status=SubagentStatus.RUNNING, recovery_required=False)
        thread = Thread(target=self._run, args=(record.request.child_id, context, runner, control), daemon=True)
        with self._lock:
            self._threads[record.request.child_id] = thread
        thread.start()

    def _run(self, child_id: str, context: OperationContext, runner: Runner, control: _Cancellation) -> None:
        record = self._repository.get(child_id)
        assert record is not None
        checkpoint = record.checkpoint
        expected = checkpoint.sequence if checkpoint else -1
        state = dict(checkpoint.state) if checkpoint else {}

        def save(next_state: Mapping[str, Any], cursor: str | None = None) -> ContinuableCheckpoint:
            nonlocal expected, state
            candidate = ContinuableCheckpoint(child_id, expected + 1, next_state, cursor)
            saved = self._repository.save_checkpoint(candidate, expected_sequence=expected)
            if saved is None:
                raise RuntimeError("checkpoint conflict")
            expected, state = saved.sequence, dict(saved.state)
            return saved

        try:
            if context.expired:
                raise TimeoutError("operation deadline expired")
            output = runner(state, save, control)
            if control.cancelled or context.cancellation.cancelled:
                raise _Cancelled(control.reason if control.cancelled else "context cancelled")
            usage = SubagentUsage(steps=expected + 1)
            result = SubagentResult(child_id, record.request.parent_id, SubagentStatus.SUCCEEDED, output=output, usage=usage)
            self._repository.update(child_id, status=result.status, usage=usage, result=result, recovery_required=False)
        except _Cancelled as exc:
            error = SubagentError("cancelled", str(exc))
            result = SubagentResult(child_id, record.request.parent_id, SubagentStatus.CANCELLED, error=error, usage=SubagentUsage(steps=max(expected + 1, 0)))
            self._repository.update(child_id, status=result.status, usage=result.usage, result=result, cancellation_reason=str(exc))
        except TimeoutError as exc:
            result = SubagentResult(child_id, record.request.parent_id, SubagentStatus.TIMED_OUT, error=SubagentError("deadline_exceeded", str(exc), True), usage=SubagentUsage(steps=max(expected + 1, 0)))
            self._repository.update(child_id, status=result.status, usage=result.usage, result=result, recovery_required=True)
        except Exception as exc:  # runner failures are durable and resumable
            result = SubagentResult(child_id, record.request.parent_id, SubagentStatus.FAILED, error=SubagentError("runner_failed", str(exc), True), usage=SubagentUsage(steps=max(expected + 1, 0)))
            self._repository.update(child_id, status=result.status, usage=result.usage, result=result, recovery_required=True)

    def snapshot(self, child_id: str) -> SubagentSnapshot:
        record = self._require(child_id)
        return SubagentSnapshot(child_id, record.request.parent_id, record.status, record.request.budget,
                                record.usage, record.cancellation_reason)

    def result(self, child_id: str, timeout: float | None = None) -> SubagentResult:
        thread = self._threads.get(child_id)
        if thread is not None:
            thread.join(timeout)
        record = self._require(child_id)
        if record.result is None:
            raise TimeoutError("subagent has not reached a terminal state")
        return record.result

    def cancel(self, child_id: str, *, reason: str = "cancellation requested") -> bool:
        self._require(child_id)
        control = self._controls.get(child_id)
        if control is None:
            return False
        return control.cancel(reason)

    def resume(self, child_id: str, context: OperationContext, runner: Runner) -> SubagentHandle:
        record = self._require(child_id)
        if not record.recovery_required:
            raise InvalidSubagentRequest("subagent is not recoverable")
        control = _Cancellation()
        with self._lock:
            self._controls[child_id] = control
        self._launch(record, context, runner, control)
        return _Handle(self, child_id, record.request.parent_id)

    def recover(self) -> tuple[str, ...]:
        """Mark orphaned running children retryable after a host restart."""
        recovered: list[str] = []
        for record in self._repository.list_recoverable():
            if record.status is SubagentStatus.RUNNING:
                self._repository.update(record.request.child_id, status=SubagentStatus.FAILED,
                                        result=SubagentResult(record.request.child_id, record.request.parent_id,
                                            SubagentStatus.FAILED, error=SubagentError("interrupted", "worker restart", True)),
                                        recovery_required=True)
                recovered.append(record.request.child_id)
        return tuple(recovered)

    def _require(self, child_id: str) -> ContinuableRecord:
        record = self._repository.get(child_id)
        if record is None:
            raise InvalidSubagentRequest(f"unknown child_id {child_id!r}")
        return record

    def close(self, timeout: float | None = None) -> bool:
        for control in tuple(self._controls.values()):
            control.cancel("service closing")
        for thread in tuple(self._threads.values()):
            thread.join(timeout)
        return not any(thread.is_alive() for thread in self._threads.values())


class _Cancelled(Exception):
    pass


class _Handle(SubagentHandle):
    def __init__(self, service: ContinuableSubagentService, child_id: str, parent_id: str) -> None:
        self._service, self._child_id, self._parent_id = service, child_id, parent_id

    @property
    def child_id(self) -> str:
        return self._child_id

    @property
    def parent_id(self) -> str:
        return self._parent_id

    def cancel(self, *, reason: str = "cancellation requested") -> bool:
        return self._service.cancel(self._child_id, reason=reason)

    def result(self, timeout: float | None = None) -> SubagentResult:
        return self._service.result(self._child_id, timeout)

    def snapshot(self) -> SubagentSnapshot:
        return self._service.snapshot(self._child_id)


__all__ = [
    "ContinuableCheckpoint", "ContinuableRecord", "ContinuableSubagentRepository",
    "ContinuableSubagentService", "InMemoryContinuableSubagentRepository", "CheckpointWriter", "Runner",
    "ContinuableSubagentState", "SubagentCheckpoint", "SubagentStore", "ResumeToken",
]
