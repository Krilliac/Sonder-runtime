"""Repository-backed child-session continuation (AGENT-006).

This boundary extends the WP5 continuable-subagent contract with durable
lineage and a real transactional repository.  A service instance owns worker
threads; the repository owns child-session metadata, checkpoints, and
cancellation intent.  A new service instance can therefore recover a child
without trusting the old process' memory.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from threading import Event, Lock, Thread
from typing import Protocol
from uuid import uuid4
from ..ports.continuation_mutations import (
    ContinuationStorageFailure, ContinuationCommitAmbiguous, ContinuationCleanupRequired,
    PreparedContinuationMutation, ContinuationMutationOutcome,
)

from ..context import OperationContext
from ..ports.subagents import (
    InvalidSubagentRequest, SubagentBudget, SubagentError, SubagentHandle,
    SubagentRequest, SubagentResult, SubagentSnapshot, SubagentStatus,
    SubagentUsage, TERMINAL_SUBAGENT_STATUSES,
)
from .continuable import ContinuableCheckpoint
from sonder_runtime.domain.agents.roles import AgentRole, role_budget


from ..ports.continuation_records import ChildSessionLineage, DurableChildSession


class DurableContinuationRepository(Protocol):
    """Transactional persistence port for child sessions."""

    def mutate(self, prepared: PreparedContinuationMutation) -> ContinuationMutationOutcome: ...
    def reconcile(self, prepared: PreparedContinuationMutation) -> ContinuationMutationOutcome | None: ...
    def read_mutation(self, operation_id: str) -> PreparedContinuationMutation | None: ...
    def latest_mutation(self, child_id: str) -> PreparedContinuationMutation | None: ...
    def unresolved_mutation(self, child_id: str) -> PreparedContinuationMutation | None: ...
    def mutation_ids(self, child_id: str, *, after: int = 0, limit: int = 100) -> tuple[tuple[tuple[int, str], ...], bool]: ...

    def create(self, session: DurableChildSession) -> DurableChildSession: ...
    def get(self, child_id: str) -> DurableChildSession | None: ...
    def save_checkpoint(self, checkpoint: ContinuableCheckpoint, *, expected_sequence: int) -> DurableChildSession | None: ...
    def update(self, child_id: str, *, status: SubagentStatus, expected_revision: int | None = None,
               usage: SubagentUsage | None = None, result: SubagentResult | None = None,
               recovery_required: bool | None = None) -> DurableChildSession | None: ...
    def claim_resume(self, child_id: str, *, expected_revision: int) -> DurableChildSession | None:
        """Atomically claim eligible recovery as RUNNING, clearing its old result."""
        ...
    def request_cancel(self, child_id: str, *, reason: str) -> bool: ...
    def list_active(self) -> tuple[DurableChildSession, ...]: ...

    def list_all(self, *, limit: int = 1000) -> tuple[DurableChildSession, ...]: ...


Runner = Callable[[Mapping[str, object], Callable[[Mapping[str, object], str | None], ContinuableCheckpoint], "DurableCancellation"], str]


class DurableCancellation:
    def __init__(self, repository: DurableContinuationRepository, child_id: str,
                 cancel_request: Callable[[str], bool] | None = None) -> None:
        self._repository, self._child_id = repository, child_id
        self._cancel_request = cancel_request
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
        self._event.set()
        if self._cancel_request is not None:
            return self._cancel_request(reason)
        return self._repository.request_cancel(self._child_id, reason=reason)

    def wait(self, timeout: float | None = None) -> bool:
        return self._event.wait(timeout) or self.cancelled


class DurableContinuationService:
    """Worker supervision over a repository-backed child-session record."""

    def __init__(self, repository: DurableContinuationRepository) -> None:
        self._repository = repository
        self._controls: dict[str, DurableCancellation] = {}
        self._threads: dict[str, Thread] = {}
        self._lock = Lock()
        self._admitted_roots: dict[str, int] = {}
        self._storage_failures: dict[str, ContinuationStorageFailure] = {}

    def _write(self, method, *args, **kwargs):
        value = args[0]
        child_id = (value.request.child_id if isinstance(value, DurableChildSession)
                    else value.child_id if isinstance(value, ContinuableCheckpoint) else value)
        try:
            self._require_storage_settled(child_id)
            return getattr(self._repository, method)(*args, **kwargs)
        except ContinuationStorageFailure as error:
            self._storage_failures[child_id] = error
            control = self._controls.get(child_id)
            if control is not None:
                control._event.set()
            raise

    def _require_storage_settled(self, child_id):
        if child_id in self._storage_failures:
            raise self._storage_failures[child_id]
        pending = self._repository.unresolved_mutation(child_id)
        if pending is not None:
            raise ContinuationCommitAmbiguous(pending)

    def spawn(self, request: SubagentRequest, context: OperationContext, runner: Runner) -> SubagentHandle:
        child_id = request.child_id or f"child-{uuid4().hex}"
        request = SubagentRequest(request.parent_id, request.prompt, request.budget, child_id, request.metadata)
        parent = self._repository.get(request.parent_id)
        # A provider root is an admission anchor whose own id is already the
        # requested parent; it must not be duplicated in a child's ancestors.
        # Ordinary parents contribute their completed chain unchanged.
        parent_is_root = parent is not None and dict(parent.request.metadata).get("provider_root") == "true"
        lineage = ChildSessionLineage(
            request.parent_id,
            () if parent_is_root else (parent.lineage.chain if parent else ()),
        )
        self._admit(request, lineage, parent)
        try:
            self._write("create", DurableChildSession(request, lineage))
        except Exception:
            self._release(lineage.chain[0])
            raise
        try:
            return self._start(child_id, context, runner)
        except Exception:
            self._release(lineage.chain[0])
            raise

    def register_root(self, root_id: str, budget: SubagentBudget) -> DurableChildSession:
        """Publish the provider-owned parent required for local children.

        A root is a durable admission anchor, not an executable child.  Keeping
        it in the same repository makes the provider's parent-existence rule
        explicit and lets nested children inherit the root ceilings.
        """
        if not isinstance(root_id, str) or not root_id.strip():
            raise InvalidSubagentRequest("root_id must be non-empty")
        request = SubagentRequest(
            parent_id=root_id,
            prompt="local provider root",
            budget=budget,
            child_id=root_id,
            metadata=(("provider_root", "true"),),
        )
        return self._write("create",
            DurableChildSession(request, ChildSessionLineage(root_id))
        )

    def require_parent(self, parent_id: str) -> DurableChildSession:
        """Return a durable parent or raise the typed unknown-id error."""
        return self._require(parent_id)

    @staticmethod
    def _metadata(request: SubagentRequest) -> dict[str, str]:
        values: dict[str, str] = {}
        for key, value in request.metadata:
            if not isinstance(key, str) or not isinstance(value, str) or key in values:
                raise InvalidSubagentRequest("subagent metadata must be unique string pairs")
            values[key] = value
        return values

    def _admit(self, request: SubagentRequest, lineage: ChildSessionLineage,
               parent: DurableChildSession | None) -> None:
        budget = request.budget
        metadata = self._metadata(request)
        role_name = metadata.get("role")
        if role_name:
            try:
                role = AgentRole(role_name)
                role_limit = role_budget(role).limit
            except (ValueError, TypeError) as exc:
                raise InvalidSubagentRequest("unknown subagent role") from exc
            role_fields = {
                "max_steps": "steps",
                "max_output_tokens": "output_tokens",
                "max_wall_seconds": "wall_seconds",
            }
            for field, role_field in role_fields.items():
                value, ceiling = getattr(budget, field), getattr(role_limit, role_field)
                if value is None or ceiling is not None and value > ceiling:
                    raise InvalidSubagentRequest(f"role budget does not admit {field}")
        root_id = lineage.chain[0]
        if budget.max_depth is not None and len(lineage.chain) > budget.max_depth:
            raise InvalidSubagentRequest("subagent depth budget exhausted")
        if parent is not None:
            from ..ports.subagents import validate_child_budget
            validate_child_budget(budget, parent.request.budget)
            children = self._repository.list_all(limit=10_000)
            direct = sum(1 for item in children if item.request.parent_id == request.parent_id)
            if budget.max_children is not None and direct >= budget.max_children:
                raise InvalidSubagentRequest("subagent child-count budget exhausted")
        with self._lock:
            active = sum(1 for item in self._repository.list_active()
                         if item.lineage.chain[0] == root_id)
            active += self._admitted_roots.get(root_id, 0)
            if budget.max_concurrency is not None and active >= budget.max_concurrency:
                raise InvalidSubagentRequest("subagent concurrency budget exhausted")
            self._admitted_roots[root_id] = self._admitted_roots.get(root_id, 0) + 1

    def _release(self, root_id: str) -> None:
        with self._lock:
            remaining = self._admitted_roots.get(root_id, 0) - 1
            if remaining > 0:
                self._admitted_roots[root_id] = remaining
            else:
                self._admitted_roots.pop(root_id, None)

    def _start(self, child_id: str, context: OperationContext, runner: Runner, *,
               resuming: bool = False) -> SubagentHandle:
        self._require_storage_settled(child_id)
        record = self._require(child_id)
        if resuming and (
            not record.recovery_required
            or record.status not in {SubagentStatus.FAILED, SubagentStatus.TIMED_OUT}
        ):
            raise InvalidSubagentRequest("child session is not recoverable")
        if record.cancellation_requested:
            raise InvalidSubagentRequest("cancelled child session cannot be started")
        if resuming:
            try:
                updated = self._write("claim_resume", child_id, expected_revision=record.revision)
            except ContinuationStorageFailure as error:
                self._storage_failures[child_id] = error
                raise
        else:
            updated = self._write("update",
                child_id, status=SubagentStatus.RUNNING,
                expected_revision=record.revision, recovery_required=False,
            )
        if (
            updated is None or updated.status is not SubagentStatus.RUNNING
            or updated.revision != record.revision + 1
            or updated.recovery_required or updated.cancellation_requested
            or updated.result is not None
        ):
            raise RuntimeError("child session state changed before launch")
        control = DurableCancellation(
            self._repository, child_id,
            lambda reason: self._write("request_cancel", child_id, reason=reason),
        )
        with self._lock:
            self._controls[child_id] = control
            thread = Thread(target=self._run, args=(child_id, context, runner, control, record.lineage.chain[0]), daemon=True)
            self._threads[child_id] = thread
        thread.start()
        return _Handle(self, child_id, record.request.parent_id)

    def _run(self, child_id, context, runner, control, root_id):
        try:
            self._run_body(child_id, context, runner, control)
        except ContinuationStorageFailure as error:
            self._storage_failures[child_id] = error
            control._event.set()
        finally:
            self._release(root_id)

    def _run_body(self, child_id: str, context: OperationContext, runner: Runner, control: DurableCancellation) -> None:
        record = self._require(child_id)
        checkpoint = record.checkpoint
        expected = checkpoint.sequence if checkpoint else -1
        state: dict[str, object] = dict(checkpoint.state) if checkpoint else {}

        def save(next_state: Mapping[str, object], cursor: str | None = None) -> ContinuableCheckpoint:
            nonlocal expected, state
            if child_id in self._storage_failures:
                raise self._storage_failures[child_id]
            candidate = ContinuableCheckpoint(child_id, expected + 1, next_state, cursor)
            try:
                saved = self._write("save_checkpoint", candidate, expected_sequence=expected)
            except ContinuationStorageFailure as error:
                self._storage_failures[child_id] = error
                control._event.set()
                raise
            if saved is None:
                raise RuntimeError("checkpoint compare-and-set conflict")
            fresh = self._require(child_id)
            expected, state = candidate.sequence, dict(candidate.state)
            return fresh.checkpoint  # type: ignore[return-value]

        try:
            if context.expired:
                raise TimeoutError("operation deadline expired")
            if control.cancelled or context.cancellation.cancelled:
                raise _Cancelled(control.reason)
            output = runner(state, save, control)
            if child_id in self._storage_failures:
                raise self._storage_failures[child_id]
            if control.cancelled or context.cancellation.cancelled:
                raise _Cancelled(control.reason)
            usage = SubagentUsage(steps=max(expected + 1, 0))
            result = SubagentResult(child_id, record.request.parent_id, SubagentStatus.SUCCEEDED, output=output, usage=usage)
            self._write("update", child_id, status=result.status, usage=usage, result=result, recovery_required=False)
        except ContinuationStorageFailure as error:
            self._storage_failures[child_id] = error
            control._event.set()
        except _Cancelled as exc:
            usage = SubagentUsage(steps=max(expected + 1, 0))
            result = SubagentResult(child_id, record.request.parent_id, SubagentStatus.CANCELLED,
                                    error=SubagentError("cancelled", str(exc)), usage=usage)
            self._write("update", child_id, status=result.status, usage=usage, result=result)
        except TimeoutError as exc:
            result = SubagentResult(child_id, record.request.parent_id, SubagentStatus.TIMED_OUT,
                                    error=SubagentError("deadline_exceeded", str(exc), True),
                                    usage=SubagentUsage(steps=max(expected + 1, 0)))
            self._write("update", child_id, status=result.status, usage=result.usage, result=result, recovery_required=True)
        except Exception as exc:
            result = SubagentResult(child_id, record.request.parent_id, SubagentStatus.FAILED,
                                    error=SubagentError("runner_failed", str(exc), True),
                                    usage=SubagentUsage(steps=max(expected + 1, 0)))
            self._write("update", child_id, status=result.status, usage=result.usage, result=result, recovery_required=True)

    def resume(self, child_id: str, context: OperationContext, runner: Runner) -> SubagentHandle:
        return self._start(child_id, context, runner, resuming=True)

    def recover_after_restart(self) -> tuple[str, ...]:
        if self._storage_failures:
            raise next(iter(self._storage_failures.values()))
        recovered: list[str] = []
        for record in self._repository.list_active():
            with self._lock:
                if record.request.child_id in self._threads and self._threads[record.request.child_id].is_alive():
                    continue
            if record.status is SubagentStatus.RUNNING:
                child_id = record.request.child_id
                pending = self._repository.unresolved_mutation(child_id)
                if pending is not None:
                    raise ContinuationCommitAmbiguous(pending)
                latest = self._repository.latest_mutation(child_id)
                if latest is not None and self._repository.reconcile(latest) is None:
                    raise ContinuationCommitAmbiguous(latest)
                raise ContinuationCleanupRequired(child_id)
        return tuple(recovered)

    def cancel(self, child_id: str, *, reason: str = "cancellation requested") -> bool:
        self._require(child_id)
        return self._write("request_cancel", child_id, reason=reason)

    def snapshot(self, child_id: str) -> SubagentSnapshot:
        record = self._require(child_id)
        return SubagentSnapshot(child_id, record.request.parent_id, record.status, record.request.budget,
                                record.usage, record.cancellation_reason)

    def result(self, child_id: str, timeout: float | None = None) -> SubagentResult:
        with self._lock:
            thread = self._threads.get(child_id)
        if thread is not None:
            thread.join(timeout)
        self._require_storage_settled(child_id)
        result = self._require(child_id).result
        if result is None:
            raise TimeoutError("child session has not reached a terminal state")
        return result

    def close(self, timeout: float | None = None) -> bool:
        for child_id in tuple(self._controls):
            self._controls[child_id]._event.set()
            try:
                self.cancel(child_id, reason="service closing")
            except ContinuationStorageFailure:
                pass  # Join owned workers, retaining the unresolved durable state.
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
