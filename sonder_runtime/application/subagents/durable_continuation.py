"""Repository-backed child-session continuation (AGENT-006).

This boundary extends the WP5 continuable-subagent contract with durable
lineage and a real transactional repository.  A service instance owns worker
threads; the repository owns child-session metadata, checkpoints, and
cancellation intent.  A new service instance can therefore recover a child
without trusting the old process' memory.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from threading import Event, Lock, Thread
from typing import Protocol
from uuid import uuid4

from ..context import OperationContext
from ..ports.subagents import (
    InvalidSubagentRequest, SubagentBudget, SubagentError, SubagentHandle,
    SubagentRequest, SubagentResult, SubagentSnapshot, SubagentStatus,
    SubagentUsage, TERMINAL_SUBAGENT_STATUSES,
)
from .continuable import ContinuableCheckpoint


@dataclass(frozen=True, slots=True)
class ChildSessionLineage:
    """Immutable parent chain captured when the child is created."""

    parent_id: str
    ancestors: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.parent_id.strip() or any(not item.strip() for item in self.ancestors):
            raise InvalidSubagentRequest("child lineage ids must be non-empty")
        if self.parent_id in self.ancestors:
            raise InvalidSubagentRequest("child lineage contains a cycle")

    @property
    def chain(self) -> tuple[str, ...]:
        return self.ancestors + (self.parent_id,)


@dataclass(frozen=True, slots=True)
class DurableChildSession:
    request: SubagentRequest
    lineage: ChildSessionLineage
    status: SubagentStatus = SubagentStatus.CREATED
    checkpoint: ContinuableCheckpoint | None = None
    revision: int = 0
    usage: SubagentUsage = SubagentUsage()
    result: SubagentResult | None = None
    recovery_required: bool = False
    cancellation_requested: bool = False
    cancellation_reason: str | None = None


class DurableContinuationRepository(Protocol):
    """Transactional persistence port for child sessions."""

    def create(self, session: DurableChildSession) -> DurableChildSession: ...
    def get(self, child_id: str) -> DurableChildSession | None: ...
    def save_checkpoint(self, checkpoint: ContinuableCheckpoint, *, expected_sequence: int) -> DurableChildSession | None: ...
    def update(self, child_id: str, *, status: SubagentStatus, expected_revision: int | None = None,
               usage: SubagentUsage | None = None, result: SubagentResult | None = None,
               recovery_required: bool | None = None) -> DurableChildSession | None: ...
    def request_cancel(self, child_id: str, *, reason: str) -> bool: ...
    def list_active(self) -> tuple[DurableChildSession, ...]: ...

    def list_all(self, *, limit: int = 1000) -> tuple[DurableChildSession, ...]: ...


Runner = Callable[[Mapping[str, object], Callable[[Mapping[str, object], str | None], ContinuableCheckpoint], "DurableCancellation"], str]


class DurableCancellation:
    def __init__(self, repository: DurableContinuationRepository, child_id: str) -> None:
        self._repository, self._child_id = repository, child_id
        self._event = Event()

    @property
    def cancelled(self) -> bool:
        if self._event.is_set():
            return True
        record = self._repository.get(self._child_id)
        return record is None or record.cancellation_requested

    @property
    def reason(self) -> str:
        record = self._repository.get(self._child_id)
        return (record.cancellation_reason if record else None) or "cancellation requested"

    def cancel(self, reason: str) -> bool:
        changed = self._repository.request_cancel(self._child_id, reason=reason)
        self._event.set()
        return changed

    def wait(self, timeout: float | None = None) -> bool:
        return self._event.wait(timeout) or self.cancelled


class DurableContinuationService:
    """Worker supervision over a repository-backed child-session record."""

    def __init__(self, repository: DurableContinuationRepository) -> None:
        self._repository = repository
        self._controls: dict[str, DurableCancellation] = {}
        self._threads: dict[str, Thread] = {}
        self._lock = Lock()

    def spawn(self, request: SubagentRequest, context: OperationContext, runner: Runner) -> SubagentHandle:
        child_id = request.child_id or f"child-{uuid4().hex}"
        request = SubagentRequest(request.parent_id, request.prompt, request.budget, child_id, request.metadata)
        parent = self._repository.get(request.parent_id)
        lineage = ChildSessionLineage(request.parent_id, parent.lineage.chain if parent else ())
        self._repository.create(DurableChildSession(request, lineage))
        return self._start(child_id, context, runner)

    def _start(self, child_id: str, context: OperationContext, runner: Runner) -> SubagentHandle:
        record = self._require(child_id)
        updated = self._repository.update(child_id, status=SubagentStatus.RUNNING, recovery_required=False)
        if updated is None:
            raise RuntimeError("child session state changed before launch")
        control = DurableCancellation(self._repository, child_id)
        with self._lock:
            self._controls[child_id] = control
            thread = Thread(target=self._run, args=(child_id, context, runner, control), daemon=True)
            self._threads[child_id] = thread
        thread.start()
        return _Handle(self, child_id, record.request.parent_id)

    def _run(self, child_id: str, context: OperationContext, runner: Runner, control: DurableCancellation) -> None:
        record = self._require(child_id)
        checkpoint = record.checkpoint
        expected = checkpoint.sequence if checkpoint else -1
        state: dict[str, object] = dict(checkpoint.state) if checkpoint else {}

        def save(next_state: Mapping[str, object], cursor: str | None = None) -> ContinuableCheckpoint:
            nonlocal expected, state
            candidate = ContinuableCheckpoint(child_id, expected + 1, next_state, cursor)
            saved = self._repository.save_checkpoint(candidate, expected_sequence=expected)
            if saved is None:
                raise RuntimeError("checkpoint compare-and-set conflict")
            fresh = self._require(child_id)
            expected, state = candidate.sequence, dict(candidate.state)
            return fresh.checkpoint  # type: ignore[return-value]

        try:
            if context.expired:
                raise TimeoutError("operation deadline expired")
            output = runner(state, save, control)
            if control.cancelled or context.cancellation.cancelled:
                raise _Cancelled(control.reason)
            usage = SubagentUsage(steps=max(expected + 1, 0))
            result = SubagentResult(child_id, record.request.parent_id, SubagentStatus.SUCCEEDED, output=output, usage=usage)
            self._repository.update(child_id, status=result.status, usage=usage, result=result, recovery_required=False)
        except _Cancelled as exc:
            usage = SubagentUsage(steps=max(expected + 1, 0))
            result = SubagentResult(child_id, record.request.parent_id, SubagentStatus.CANCELLED,
                                    error=SubagentError("cancelled", str(exc)), usage=usage)
            self._repository.update(child_id, status=result.status, usage=usage, result=result)
        except TimeoutError as exc:
            result = SubagentResult(child_id, record.request.parent_id, SubagentStatus.TIMED_OUT,
                                    error=SubagentError("deadline_exceeded", str(exc), True),
                                    usage=SubagentUsage(steps=max(expected + 1, 0)))
            self._repository.update(child_id, status=result.status, usage=result.usage, result=result, recovery_required=True)
        except Exception as exc:
            result = SubagentResult(child_id, record.request.parent_id, SubagentStatus.FAILED,
                                    error=SubagentError("runner_failed", str(exc), True),
                                    usage=SubagentUsage(steps=max(expected + 1, 0)))
            self._repository.update(child_id, status=result.status, usage=result.usage, result=result, recovery_required=True)

    def resume(self, child_id: str, context: OperationContext, runner: Runner) -> SubagentHandle:
        record = self._require(child_id)
        if not record.recovery_required:
            raise InvalidSubagentRequest("child session is not recoverable")
        if record.cancellation_requested:
            raise InvalidSubagentRequest("cancelled child session cannot be resumed")
        return self._start(child_id, context, runner)

    def recover_after_restart(self) -> tuple[str, ...]:
        recovered: list[str] = []
        for record in self._repository.list_active():
            with self._lock:
                if record.request.child_id in self._threads and self._threads[record.request.child_id].is_alive():
                    continue
            if record.status is SubagentStatus.RUNNING:
                result = SubagentResult(record.request.child_id, record.request.parent_id, SubagentStatus.FAILED,
                                        error=SubagentError("interrupted", "worker restart", True),
                                        usage=record.usage)
                if self._repository.update(record.request.child_id, status=SubagentStatus.FAILED,
                                           expected_revision=record.revision, result=result,
                                           recovery_required=True):
                    recovered.append(record.request.child_id)
        return tuple(recovered)

    def cancel(self, child_id: str, *, reason: str = "cancellation requested") -> bool:
        self._require(child_id)
        return self._repository.request_cancel(child_id, reason=reason)

    def snapshot(self, child_id: str) -> SubagentSnapshot:
        record = self._require(child_id)
        return SubagentSnapshot(child_id, record.request.parent_id, record.status, record.request.budget,
                                record.usage, record.cancellation_reason)

    def result(self, child_id: str, timeout: float | None = None) -> SubagentResult:
        with self._lock:
            thread = self._threads.get(child_id)
        if thread is not None:
            thread.join(timeout)
        result = self._require(child_id).result
        if result is None:
            raise TimeoutError("child session has not reached a terminal state")
        return result

    def close(self, timeout: float | None = None) -> bool:
        for child_id in tuple(self._controls):
            self.cancel(child_id, reason="service closing")
        with self._lock:
            threads = tuple(self._threads.values())
        for thread in threads:
            thread.join(timeout)
        return not any(thread.is_alive() for thread in threads)

    def _require(self, child_id: str) -> DurableChildSession:
        record = self._repository.get(child_id)
        if record is None:
            raise InvalidSubagentRequest(f"unknown child_id {child_id!r}")
        return record


class _Cancelled(Exception):
    pass


class _Handle(SubagentHandle):
    def __init__(self, service: DurableContinuationService, child_id: str, parent_id: str) -> None:
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
    "ChildSessionLineage", "DurableChildSession", "DurableContinuationRepository",
    "DurableCancellation", "DurableContinuationService",
]
