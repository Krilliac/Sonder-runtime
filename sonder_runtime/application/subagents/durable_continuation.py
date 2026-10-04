"""Repository-backed child-session continuation (AGENT-006).

This boundary extends the WP5 continuable-subagent contract with durable
lineage and a real transactional repository.  A service instance owns worker
threads; the repository owns child-session metadata, checkpoints, and
cancellation intent.  A new service instance can therefore recover a child
without trusting the old process' memory.
"""
from __future__ import annotations

from sonder_runtime.application.ports.runtime_threads import Thread as owned_runtime_thread

from collections.abc import Callable, Mapping
from dataclasses import replace
from threading import Event, Lock, Thread
from time import monotonic, sleep
import os
import platform
import json
from typing import Protocol
from uuid import uuid4
from ..ports.continuation_mutations import (
    ContinuationStorageFailure, ContinuationCommitAmbiguous, ContinuationCleanupRequired,
    PreparedContinuationMutation, ContinuationMutationOutcome,
)

from ..context import OperationContext
from ..execution.resume_reality import (
    RESUME_REALITY_CONTEXT_KEY,
    RESUME_REALITY_HOST_KEY,
    ResumeBarrier,
    bound as bound_resume_barrier,
)
from ..ports.workspace_reality import WorkspaceRealityPort, scope_intersects
from ..ports.subagents import (
    InvalidSubagentRequest, SubagentBudget, SubagentError, SubagentHandle,
    SubagentRequest, SubagentResult, SubagentSnapshot, SubagentStatus,
    SubagentUsage, TERMINAL_SUBAGENT_STATUSES,
)
from .continuable import (
    ContinuableCheckpoint, checkpoint_state_digest, provenance_subject_error,
)
from .checkpoint_provenance import (
    CheckpointProvenanceError, CheckpointProvenanceHook, ProvenanceSubject,
)
from sonder_runtime.domain.agents.roles import AgentRole, role_budget
from sonder_runtime.application.owner_process import recorded_owner_is_dead


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
    def get_active_by_key(self, parent_id: str, key: str, namespace: str) -> DurableChildSession | None: ...
    def get_by_key(self, parent_id: str, key: str, namespace: str) -> DurableChildSession | None: ...
    def save_checkpoint(self, checkpoint: ContinuableCheckpoint, *, expected_sequence: int) -> DurableChildSession | None: ...
    def update(self, child_id: str, *, status: SubagentStatus, expected_revision: int | None = None,
               usage: SubagentUsage | None = None, result: SubagentResult | None = None,
               recovery_required: bool | None = None, metadata: tuple[tuple[str, str], ...] | None = None,
               verification: Mapping[str, object] | None = None) -> DurableChildSession | None: ...
    def claim_resume(self, child_id: str, *, expected_revision: int) -> DurableChildSession | None:
        """Atomically claim eligible recovery as RUNNING, clearing its old result."""
        ...
    def request_cancel(self, child_id: str, *, reason: str,
                       expected_revision: int | None = None,
                       unstarted_only: bool = False) -> bool: ...
    def list_active(self) -> tuple[DurableChildSession, ...]: ...

    def list_all(self, *, limit: int = 1000) -> tuple[DurableChildSession, ...]: ...


Runner = Callable[[Mapping[str, object], Callable[[Mapping[str, object], str | None], ContinuableCheckpoint], "DurableCancellation"], str]

# A second repository process can have durably retained an intent while it is
# still finishing the effect receipt.  Cancellation is cooperative, so give
# that ordered write a short, bounded chance to settle before publishing the
# terminal cancellation result.  An absent receipt after the grace period is
# still an ambiguous storage outcome and remains fail-closed.
_CANCELLATION_SETTLEMENT_GRACE_SECONDS = 1.0


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

    def __init__(self, repository: DurableContinuationRepository, *,
                 checkpoint_provenance: CheckpointProvenanceHook | None = None,
                 workspace_reality: WorkspaceRealityPort | None = None) -> None:
        if checkpoint_provenance is not None and not callable(checkpoint_provenance):
            raise TypeError("checkpoint provenance hook must be callable")
        self._repository = repository
        # Host-owned provenance source.  Runner code only supplies state and a
        # cursor; the service stamps provenance after the journal read and
        # before the child compare-and-set.  Without a hook every checkpoint
        # is stored provenance-absent and cannot authorize resume-from-state.
        self._checkpoint_provenance = checkpoint_provenance
        self._workspace_reality = workspace_reality
        self._controls: dict[str, DurableCancellation] = {}
        self._threads: dict[str, Thread] = {}
        self._lock = Lock()
        self._spawn_lock = Lock()
        self._contexts: dict[str, OperationContext] = {}
        self._storage_failures: dict[str, ContinuationStorageFailure] = {}
        # A reservation is consumable only by the provider instance that
        # created it.  This is deliberately process-scoped; restart recovery
        # remains an explicit path and never happens from ``spawn``.
        self._owner_nonce = uuid4().hex
        self._owner_pid = os.getpid()
        self._owner_host = platform.node()

    @property
    def owner_nonce(self) -> str:
        return self._owner_nonce

    @property
    def owner_pid(self) -> int:
        return self._owner_pid

    @property
    def owner_host(self) -> str:
        return self._owner_host

    @staticmethod
    def bounded_context(context: OperationContext, budget: SubagentBudget, *,
                        started_at: float | None = None) -> OperationContext:
        """Narrow an attempt to its wall budget and the parent deadline."""
        if budget.max_wall_seconds is None:
            return context
        deadline = (monotonic() if started_at is None else started_at) + budget.max_wall_seconds
        if context.deadline_monotonic is not None:
            deadline = min(deadline, context.deadline_monotonic)
        return replace(context, deadline_monotonic=deadline)

    def _write(self, method, *args, _settlement_timeout=0.0, **kwargs):
        value = args[0]
        child_id = (value.request.child_id if isinstance(value, DurableChildSession)
                    else value.child_id if isinstance(value, ContinuableCheckpoint) else value)
        try:
            # The repository records intent before its effect receipt. Keep the
            # settlement check and the following mutation together so a worker
            # cannot observe another operation in that narrow interval and
            # misclassify the ordered mutation as its own ambiguity.
            with self._lock:
                if _settlement_timeout > 0:
                    self._wait_for_storage_settlement(
                        child_id, timeout=_settlement_timeout
                    )
                else:
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

    def _wait_for_storage_settlement(self, child_id, *, timeout: float) -> None:
        """Wait briefly for an already-retained external intent to receipt.

        The repository intentionally separates intent retention from the
        effect receipt so a crashed writer can be reconciled.  A cooperative
        cancellation worker may therefore encounter a healthy writer in that
        narrow interval.  Polling reconciliation here does not retry an
        unknown effect: it only proceeds once the exact retained operation has
        a receipt, and otherwise raises the same ambiguity after a finite
        deadline.
        """
        deadline = monotonic() + max(0.0, timeout)
        while True:
            if child_id in self._storage_failures:
                raise self._storage_failures[child_id]
            pending = self._repository.unresolved_mutation(child_id)
            if pending is None:
                return
            if self._repository.reconcile(pending) is not None:
                continue
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise ContinuationCommitAmbiguous(pending)
            sleep(min(0.01, remaining))

    def spawn(self, request: SubagentRequest, context: OperationContext, runner: Runner) -> SubagentHandle:
        child_id = request.child_id or f"child-{uuid4().hex}"
        request = SubagentRequest(request.parent_id, request.prompt, request.budget, child_id, request.metadata, request.resume_key, request.idempotency_key)
        with self._spawn_lock:
            existing = self._repository.get(child_id)
            if existing is None:
                lookup = getattr(self._repository, "get_active_by_key", None)
                if callable(lookup):
                    for key, namespace in ((request.resume_key, "resume"), (request.idempotency_key, "idempotency")):
                        if key:
                            existing = lookup(request.parent_id, key, namespace)
                            if existing is not None:
                                break
            if existing is None:
                lookup = getattr(self._repository, "get_by_key", None)
                if callable(lookup):
                    for key, namespace in ((request.resume_key, "resume"), (request.idempotency_key, "idempotency")):
                        if key:
                            existing = lookup(request.parent_id, key, namespace)
                            if existing is not None:
                                break
            if existing is not None and existing.status in {SubagentStatus.CREATED, SubagentStatus.QUEUED, SubagentStatus.RUNNING}:
                same_scope = (
                    existing.request.parent_id == request.parent_id
                    and existing.request.prompt == request.prompt
                    and existing.request.budget == request.budget
                    and existing.request.metadata == request.metadata
                    and existing.request.resume_key == request.resume_key
                    and existing.request.idempotency_key == request.idempotency_key
                )
                if (not request.resume_key or not request.idempotency_key or not same_scope):
                    raise InvalidSubagentRequest("active child identity or scope does not match requested delegation")
                existing_child_id = existing.request.child_id
                existing_metadata = self._metadata(existing.request)
                # A continuation-backed worker registry may have durably
                # reserved this exact child before the provider thread was
                # created.  Consume that reservation only for the same
                # authenticated owner; another process must use explicit
                # recovery rather than silently taking over a live worker.
                if (
                    existing.status is SubagentStatus.CREATED
                    and existing_metadata.get("worker_registry_admitted") == "true"
                ):
                    owner_nonce = existing_metadata.get("owner_nonce")
                    owner_dead = (
                        owner_nonce
                        and owner_nonce != self._owner_nonce
                        and recorded_owner_is_dead(existing_metadata)
                    )
                    if (
                        owner_nonce
                        and owner_nonce != self._owner_nonce
                        and not owner_dead
                    ) or (
                        not owner_nonce
                        and existing_metadata.get("owner_id") != context.principal_id
                    ):
                        raise InvalidSubagentRequest("worker reservation belongs to another owner")
                    parent = self._repository.get(request.parent_id)
                    self._admit(
                        request,
                        existing.lineage,
                        parent,
                    )
                    return self._start(existing_child_id, context, runner)
                with self._lock:
                    thread = self._threads.get(existing_child_id)
                if thread is not None and thread.is_alive():
                    with self._lock:
                        original_context = self._contexts.get(existing_child_id)
                    if original_context is None or not self._compatible_context(original_context, context):
                        raise InvalidSubagentRequest("active child operation scope is incompatible")
                    return _Handle(self, existing_child_id, request.parent_id)
                raise InvalidSubagentRequest("active child requires recover/resume after restart")
            if existing is not None and existing.status in TERMINAL_SUBAGENT_STATUSES:
                same_scope = (
                    existing.request.parent_id == request.parent_id
                    and existing.request.prompt == request.prompt
                    and existing.request.budget == request.budget
                    and existing.request.metadata == request.metadata
                    and existing.request.resume_key == request.resume_key
                    and existing.request.idempotency_key == request.idempotency_key
                )
                if not same_scope:
                    raise InvalidSubagentRequest(
                        "terminal child identity or scope does not match requested delegation"
                    )
                if not self._terminal_context_compatible(existing.request, context):
                    raise InvalidSubagentRequest("terminal child operation scope cannot be proven")
                if existing.recovery_required:
                    raise InvalidSubagentRequest(
                        "terminal child requires explicit resume after recovery"
                    )
                # The durable terminal result is the authoritative reuse
                # value. Return a handle backed by the repository and do not
                # admit a second worker or invoke the runner again.
                return _Handle(self, existing.request.child_id, request.parent_id)
            parent = self._repository.get(request.parent_id)
            # A provider root is an admission anchor whose own id is already the
            # requested parent; it must not be duplicated in a child's ancestors.
            parent_is_root = (
                parent is not None
                and parent.request.child_id == parent.request.parent_id
                and dict(parent.request.metadata).get("provider_root") == "true"
            )
            lineage = ChildSessionLineage(
                request.parent_id,
                () if parent_is_root else (parent.lineage.chain if parent else ()),
            )
            self._admit(request, lineage, parent)
            # The canonical repository counts this reservation and every
            # registry-created reservation under its cross-process writer lock.
            self._write("create", DurableChildSession(request, lineage))
            return self._start(child_id, context, runner)

    def register_root(self, root_id: str, budget: SubagentBudget, *,
                      owner_id: str = "") -> DurableChildSession:
        """Publish the provider-owned parent required for local children.

        A root is a durable admission anchor, not an executable child.  Keeping
        it in the same repository makes the provider's parent-existence rule
        explicit and lets nested children inherit the root ceilings.
        """
        if not isinstance(root_id, str) or not root_id.strip():
            raise InvalidSubagentRequest("root_id must be non-empty")
        if (not isinstance(owner_id, str) or len(owner_id) > 256
                or owner_id and not owner_id.strip()):
            raise InvalidSubagentRequest("root owner_id must be bounded text")
        request = SubagentRequest(
            parent_id=root_id,
            prompt="local provider root",
            budget=budget,
            child_id=root_id,
            metadata=(("provider_root", "true"),) + (
                (("owner_id", owner_id),) if owner_id else ()
            ),
        )
        def existing_root() -> DurableChildSession | None:
            record = self._repository.get(root_id)
            if record is None:
                return None
            if (record.request != request or record.lineage != ChildSessionLineage(root_id)
                    or record.status is not SubagentStatus.CREATED
                    or record.cancellation_requested):
                raise InvalidSubagentRequest("registered root owner or budget differs")
            return record

        existing = existing_root()
        if existing is not None:
            return existing
        try:
            return self._write("create", DurableChildSession(request, ChildSessionLineage(root_id)))
        except InvalidSubagentRequest:
            # Another service can publish the same root after our read. Only
            # an exact immutable match makes that race idempotent.
            existing = existing_root()
            if existing is not None:
                return existing
            raise

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
        if budget.max_depth is not None and len(lineage.chain) > budget.max_depth:
            raise InvalidSubagentRequest("subagent depth budget exhausted")
        if parent is not None:
            from ..ports.subagents import validate_child_budget
            validate_child_budget(budget, parent.request.budget)

    def _start(self, child_id: str, context: OperationContext, runner: Runner, *,
               resuming: bool = False, expected_revision: int | None = None,
               resume_barrier: ResumeBarrier | None = None) -> SubagentHandle:
        self._require_storage_settled(child_id)
        record = self._require(child_id)
        if expected_revision is not None and record.revision != expected_revision:
            raise InvalidSubagentRequest("child session changed after resume validation")
        if resuming and (
            not record.recovery_required
            or record.status not in {SubagentStatus.FAILED, SubagentStatus.TIMED_OUT}
        ):
            raise InvalidSubagentRequest("child session is not recoverable")
        if record.cancellation_requested:
            raise InvalidSubagentRequest("cancelled child session cannot be started")
        budget = record.request.budget
        if budget.max_wall_seconds is not None:
            remaining = budget.max_wall_seconds - (record.usage.wall_seconds or 0)
            if remaining <= 0:
                raise InvalidSubagentRequest("subagent wall budget exhausted")
            budget = replace(budget, max_wall_seconds=remaining)
        started_at = monotonic()
        context = self.bounded_context(context, budget, started_at=started_at)
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
            thread = owned_runtime_thread(target=self._run, args=(child_id, context, runner, control, started_at, resume_barrier), daemon=True)
            self._threads[child_id] = thread
        with self._lock:
            self._contexts[child_id] = context
        thread.start()
        return _Handle(self, child_id, record.request.parent_id)

    @staticmethod
    def _compatible_context(original: OperationContext, current: OperationContext) -> bool:
        if (current.expired or original.expired
                or getattr(current.cancellation, "cancelled", False)
                or getattr(original.cancellation, "cancelled", False)):
            return False
        for name in ("principal_id", "auth_level", "source", "cloud_allowed", "remote_ollama_allowed", "session_id"):
            if getattr(original, name, None) != getattr(current, name, None):
                return False
        if tuple(original.workspace_roots) != tuple(current.workspace_roots):
            return False
        old_deadline, new_deadline = original.deadline_monotonic, current.deadline_monotonic
        if old_deadline is None:
            return new_deadline is None
        return new_deadline is None or new_deadline >= old_deadline

    @staticmethod
    def _terminal_context_compatible(request: SubagentRequest, context: OperationContext) -> bool:
        metadata = dict(request.metadata)
        if metadata.get("owner_id") != getattr(context, "principal_id", ""):
            return False
        stored_roots = tuple(filter(None, metadata.get("context_workspace_roots", "").split("|")))
        current_roots = tuple(getattr(context, "workspace_roots", ()))
        if not stored_roots or tuple(map(str, current_roots)) != stored_roots:
            return False
        for name in ("cloud_allowed", "remote_ollama_allowed", "session_id"):
            if metadata.get("context_" + name, "") != str(getattr(context, name, "")):
                return False
        return not getattr(context, "expired", False) and not getattr(
            getattr(context, "cancellation", None), "cancelled", False
        )

    def _run(self, child_id, context, runner, control, started_at, barrier=None):
        try:
            self._run_body(child_id, context, runner, control, started_at, barrier)
        except ContinuationStorageFailure as error:
            self._storage_failures[child_id] = error
            control._event.set()
        finally:
            with self._lock:
                self._contexts.pop(child_id, None)

    def _run_body(self, child_id: str, context: OperationContext, runner: Runner,
                  control: DurableCancellation, started_at: float,
                  barrier: ResumeBarrier | None = None) -> None:
        record = self._require(child_id)
        checkpoint = record.checkpoint
        expected = checkpoint.sequence if checkpoint else -1
        state: dict[str, object] = dict(checkpoint.state) if checkpoint else {}

        def usage(output: str | None = None) -> SubagentUsage:
            output_tokens = record.usage.output_tokens
            if output_tokens is None and record.usage.wall_seconds is None:
                output_tokens = 0
            if output is not None and (
                record.usage.wall_seconds is None or record.usage.output_tokens is not None
            ):
                output_tokens = (record.usage.output_tokens or 0) + (len(output.encode("utf-8")) + 3) // 4
            return SubagentUsage(
                steps=max(expected + 1, record.usage.steps, 0),
                # Output tokens use the provider's conservative four-byte
                # estimate. Unknown consumption on a prior failed attempt
                # stays unknown; it cannot silently return parent capacity.
                output_tokens=output_tokens,
                wall_seconds=(record.usage.wall_seconds or 0.0) + max(0.0, monotonic() - started_at),
            )

        def save(next_state: Mapping[str, object], cursor: str | None = None) -> ContinuableCheckpoint:
            nonlocal expected, state
            if child_id in self._storage_failures:
                raise self._storage_failures[child_id]
            checkpoint_state = dict(next_state)
            checkpoint_state.pop(RESUME_REALITY_CONTEXT_KEY, None)
            checkpoint_state = self._stamp_workspace_state(checkpoint_state, context)
            if barrier is not None and barrier.blocked:
                host = dict(checkpoint_state.get(RESUME_REALITY_HOST_KEY, {}))
                host["pending_delta"] = dict(barrier.delta or {})
                checkpoint_state[RESUME_REALITY_HOST_KEY] = host
            candidate = self._stamp_checkpoint(
                ContinuableCheckpoint(child_id, expected + 1, checkpoint_state, cursor)
            )
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

        output: str | None = None
        try:
            if context.expired:
                raise TimeoutError("operation deadline expired")
            if control.cancelled or context.cancellation.cancelled:
                raise _Cancelled(control.reason)
            if barrier is not None:
                if barrier.delta is not None:
                    state[RESUME_REALITY_CONTEXT_KEY] = barrier.delta
            with bound_resume_barrier(barrier):
                output = runner(state, save, control)
            if child_id in self._storage_failures:
                raise self._storage_failures[child_id]
            if control.cancelled or context.cancellation.cancelled:
                raise _Cancelled(control.reason)
            if context.expired:
                raise TimeoutError("subagent wall budget or operation deadline expired")
            elapsed = usage(output if isinstance(output, str) else None)
            ceiling = record.request.budget.max_output_tokens
            if ceiling is not None and elapsed.output_tokens is not None and elapsed.output_tokens > ceiling:
                raise TimeoutError("subagent output budget exhausted")
            result = SubagentResult(child_id, record.request.parent_id, SubagentStatus.SUCCEEDED, output=output, usage=elapsed)
            settled = self._write("update", child_id, status=result.status, usage=elapsed, result=result, recovery_required=False)
            if settled is None:
                current = self._require(child_id)
                if current.cancellation_requested:
                    raise _Cancelled(current.cancellation_reason or "cancellation requested")
                raise RuntimeError("child session state changed before success was recorded")
        except ContinuationStorageFailure as error:
            self._storage_failures[child_id] = error
            control._event.set()
        except _Cancelled as exc:
            elapsed = usage()
            result = SubagentResult(child_id, record.request.parent_id, SubagentStatus.CANCELLED,
                                    error=SubagentError("cancelled", str(exc)), usage=elapsed)
            self._write(
                "update",
                child_id,
                status=result.status,
                usage=elapsed,
                result=result,
                _settlement_timeout=_CANCELLATION_SETTLEMENT_GRACE_SECONDS,
            )
        except InvalidSubagentRequest as exc:
            if output is None:
                status, code, recoverable = SubagentStatus.FAILED, "runner_failed", True
            else:
                status, code, recoverable = SubagentStatus.TIMED_OUT, "budget_exhausted", False
            result = SubagentResult(child_id, record.request.parent_id, status,
                                    error=SubagentError(code, str(exc), recoverable), usage=usage(output))
            self._write("update", child_id, status=status, usage=result.usage,
                        result=result, recovery_required=recoverable)
        except TimeoutError as exc:
            result = SubagentResult(child_id, record.request.parent_id, SubagentStatus.TIMED_OUT,
                                    error=SubagentError("deadline_exceeded", str(exc), True),
                                    usage=usage())
            self._write("update", child_id, status=result.status, usage=result.usage, result=result, recovery_required=True)
        except Exception as exc:
            result = SubagentResult(child_id, record.request.parent_id, SubagentStatus.FAILED,
                                    error=SubagentError("runner_failed", str(exc), True),
                                    usage=usage())
            self._write("update", child_id, status=result.status, usage=result.usage, result=result, recovery_required=True)

    def _stamp_checkpoint(self, candidate: ContinuableCheckpoint) -> ContinuableCheckpoint:
        """Attach host provenance to a runner-proposed checkpoint.

        The hook reads the effect journal before the child compare-and-set, so
        the stamped position only names receipts that already committed.  A
        hook failure or a record for a different subject fails the save; the
        child keeps its previous checkpoint.
        """
        hook = self._checkpoint_provenance
        if hook is None:
            return candidate
        subject = ProvenanceSubject(
            candidate.child_id, candidate.sequence,
            checkpoint_state_digest(candidate.state), candidate.cursor,
        )
        provenance = hook(subject)
        stamped = ContinuableCheckpoint(
            candidate.child_id, candidate.sequence, candidate.state,
            candidate.cursor, provenance,
        )
        if provenance is None or provenance_subject_error(stamped) is not None:
            raise CheckpointProvenanceError("checkpoint provenance hook returned a foreign record")
        return stamped

    def _stamp_workspace_state(self, state: Mapping[str, object], context: OperationContext) -> dict[str, object]:
        """Capture host workspace identity before provenance hashes the state."""
        stamped = dict(state)
        stamped.pop(RESUME_REALITY_HOST_KEY, None)
        if self._workspace_reality is None:
            return stamped
        roots: dict[str, object] = {}
        deadline = min(monotonic() + 10.0, context.deadline_monotonic or float("inf"))
        for root in getattr(context, "workspace_roots", ()):
            try:
                snapshot = self._workspace_reality.capture(str(root), deadline_monotonic=deadline)
            except Exception as exc:  # noqa: BLE001 - revalidation must degrade, never block resume
                snapshot = {"status": "unavailable", "reason": type(exc).__name__}
            if snapshot is not None:
                roots[str(root)] = snapshot
        if roots:
            stamped[RESUME_REALITY_HOST_KEY] = {"roots": roots}
        else:
            stamped.pop(RESUME_REALITY_HOST_KEY, None)
        return stamped

    def _resume_reality(self, record: DurableChildSession, context: OperationContext) -> dict[str, object] | None:
        if self._workspace_reality is None:
            return None
        checkpoint = record.checkpoint
        host = dict(checkpoint.state).get(RESUME_REALITY_HOST_KEY, {}) if checkpoint else {}
        roots = host.get("roots", {}) if isinstance(host, Mapping) else {}
        if not isinstance(roots, Mapping):
            roots = {}
        pending = host.get("pending_delta") if isinstance(host, Mapping) else None
        metadata = self._metadata(record.request)
        owned_raw = metadata.get("owned_paths") or metadata.get("owned_files") or ""
        try:
            parsed = json.loads(owned_raw) if owned_raw else ()
            owned_paths = tuple(str(item) for item in parsed) if isinstance(parsed, list) else tuple(item for item in owned_raw.split("|") if item)
        except (TypeError, ValueError):  # noqa: B902 - malformed metadata is conservatively bounded
            owned_paths = tuple(item for item in owned_raw.split("|") if item)
        deltas: list[Mapping[str, object]] = []
        deadline = min(monotonic() + 10.0, context.deadline_monotonic or float("inf"))
        for root in getattr(context, "workspace_roots", ()):
            snapshot = roots.get(str(root))
            if snapshot is None:
                # ``None`` from capture denotes a non-Git workspace.  Probe it
                # on resume so legacy Git checkpoints remain fail-closed while
                # non-Git workers retain their historical behavior.
                try:
                    current = self._workspace_reality.capture(str(root), deadline_monotonic=deadline)
                except Exception as exc:  # noqa: BLE001 - bounded fail-closed delta
                    deltas.append({"status": "delta_unavailable", "requires_reinspection": True, "requires_replan": True, "reason": type(exc).__name__, "files": []})
                    continue
                if current is None:
                    continue
                deltas.append({"status": "unknown", "requires_reinspection": True, "requires_replan": True, "files": []})
                continue
            try:
                delta = self._workspace_reality.revalidate(str(root), snapshot, owned_paths,
                                                             deadline_monotonic=deadline)
            except Exception as exc:  # noqa: BLE001 - adapter failures degrade to reinspection
                delta = {"status": "delta_unavailable", "requires_reinspection": True, "requires_replan": True, "reason": type(exc).__name__, "files": []}
            else:
                if delta is not None and delta.get("status") not in {"unchanged", "same"} and (
                    bool(delta.get("history_rewritten"))
                    or scope_intersects(owned_paths, delta.get("files", ()), str(root))
                ):
                    delta = dict(delta)
                    delta["requires_replan"] = True
            if delta:
                deltas.append({"workspace_root": str(root), **delta})
        if not deltas:
            return dict(pending) if isinstance(pending, Mapping) else None
        result = {
            "status": "changed" if any(d.get("status") == "changed" for d in deltas) else deltas[0].get("status", "unknown"),
            "requires_reinspection": any(bool(d.get("requires_reinspection")) for d in deltas),
            "requires_replan": any(bool(d.get("requires_replan")) for d in deltas),
            "history_rewritten": any(bool(d.get("history_rewritten")) for d in deltas),
            "files": [item for d in deltas for item in d.get("files", ())][:400],
            "commits": [item for d in deltas for item in d.get("commits", ())][:25],
            "reason": next((d.get("reason") for d in deltas if d.get("reason")), ""),
            "workspaces": [{key: value for key, value in d.items() if key != "snapshot"}
                           for d in deltas[:16]],
        }
        if isinstance(pending, Mapping):
            result["requires_reinspection"] = True
            result["requires_replan"] = bool(pending.get("requires_replan")) or result["requires_replan"]
        return result

    def resume(self, child_id: str, context: OperationContext, runner: Runner, *,
               expected_revision: int | None = None) -> SubagentHandle:
        """Claim a recoverable child and start ``runner`` from its checkpoint.

        ``expected_revision`` pins the exact durable record a caller validated
        (checkpoint saves advance the revision), so a checkpoint cannot change
        between validation and the claim.
        """
        record = self._require(child_id)
        if expected_revision is not None and record.revision != expected_revision:
            raise InvalidSubagentRequest("child session changed before resume reality validation")
        parent = self._repository.get(record.request.parent_id)
        self._admit(record.request, record.lineage, parent)
        delta = self._resume_reality(record, context)
        return self._start(child_id, context, runner, resuming=True,
                           expected_revision=record.revision, resume_barrier=ResumeBarrier(delta))

    def record(self, child_id: str) -> DurableChildSession | None:
        """Return the durable child record, or ``None`` when it does not exist."""
        return self._repository.get(child_id)

    def runner_alive(self, child_id: str) -> bool:
        with self._lock:
            thread = self._threads.get(child_id)
        return thread is not None and thread.is_alive()

    def release_dead_owner(self, child_id: str) -> DurableChildSession:
        """Mark a RUNNING child whose recorded owner process is dead as recoverable.

        Only a worker-registry reservation records the owner's pid and host.
        The owner must be another service instance and must be proven dead;
        anything unprovable raises ``ContinuationCleanupRequired`` so the child
        stays fenced.  The transition is a revision-checked durable update to
        FAILED with ``recovery_required``; it never starts a runner.
        """
        record = self._require(child_id)
        if record.status is not SubagentStatus.RUNNING:
            return record
        if self.runner_alive(child_id):
            raise InvalidSubagentRequest("child runner is live in this service")
        metadata = self._metadata(record.request)
        owner_nonce = metadata.get("owner_nonce", "")
        if (
            metadata.get("worker_registry_admitted") != "true"
            or not owner_nonce or owner_nonce == self._owner_nonce
            or not recorded_owner_is_dead(metadata)
        ):
            raise ContinuationCleanupRequired(child_id)
        self._require_storage_settled(child_id)
        result = SubagentResult(
            child_id, record.request.parent_id, SubagentStatus.FAILED,
            error=SubagentError("owner_lost", "child owner process exited before completion", True),
            usage=record.usage,
        )
        updated = self._write(
            "update", child_id, status=SubagentStatus.FAILED,
            expected_revision=record.revision, usage=record.usage, result=result,
            recovery_required=True,
        )
        if (updated is None or updated.status is not SubagentStatus.FAILED
                or not updated.recovery_required):
            raise ContinuationCleanupRequired(child_id)
        return updated

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

    def cancel_unstarted(self, child_id: str, *, expected_revision: int,
                         reason: str = "cancellation requested") -> bool:
        """Close one reserved child only if this exact prestart revision remains."""
        return self._write(
            "request_cancel", child_id, reason=reason,
            expected_revision=expected_revision, unstarted_only=True,
        )

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
