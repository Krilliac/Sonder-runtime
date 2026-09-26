"""Durable, continuable child-agent execution (WP5 SUBAGENT-001).

The service owns lifecycle and concurrency rules; a repository owns durability.
The in-memory repository is deliberately a small test/reference adapter, not a
claim that process memory is durable.  A production adapter can implement the
same repository protocol with SQLite or another transactional store.
"""
from __future__ import annotations

from sonder_runtime.application.ports.runtime_threads import Thread as owned_runtime_thread

from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import json
import re
from threading import Event, Lock, Thread
from typing import Any, Protocol
from uuid import uuid4

from ..context import OperationContext
from ..ports.subagents import (
    InvalidSubagentRequest, SubagentBudget, SubagentError, SubagentHandle,
    SubagentRequest, SubagentResult, SubagentSnapshot, SubagentStatus,
    SubagentUsage, TERMINAL_SUBAGENT_STATUSES,
)


# Version 2 adds ``gateway_call_ordinal``; version 1 records (written before
# it existed) stay readable and digest-valid only with an ordinal of zero.
PROVENANCE_VERSION = 2
_GATEWAY_ORDINAL_VERSION = 2
_DIGEST = re.compile(r"[0-9a-f]{64}")
_MAX_PROVENANCE_TEXT = 512


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def checkpoint_state_digest(state: Mapping[str, Any]) -> str:
    """Return the SHA-256 of a checkpoint state's canonical JSON encoding.

    The digest is taken over the JSON form because that is what every child
    store persists; a tuple and the list it round-trips to digest equally.
    """
    if not isinstance(state, Mapping):
        raise InvalidSubagentRequest("checkpoint state must be a mapping")
    try:
        encoded = _canonical_bytes(dict(state))
    except (TypeError, ValueError) as exc:
        raise InvalidSubagentRequest("checkpoint state must be canonical JSON") from exc
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class CheckpointProvenance:
    """Host-stamped, immutable binding of a child checkpoint to an effect journal.

    It binds the checkpoint subject (child id, sequence, canonical state digest
    and cursor) to the journal identity, run, worker, owner epoch and settled
    journal position observed before the child compare-and-set.  From version
    2 it also records ``gateway_call_ordinal``: how many deterministic gateway
    call ordinals the child runner had issued when the checkpoint was saved,
    so a runner resumed from it re-issues later calls at the same ordinals.
    Construction checks only shape: a row read back from storage must stay readable even
    when tampered, so that the resume validator can refuse it with a typed
    reason instead of failing every read of the child.  ``record_digest``
    covers every other field; ``digest_valid`` recomputes it.
    """

    child_id: str
    sequence: int
    state_digest: str
    cursor: str | None
    journal_identity: str
    run_id: str
    worker_id: str
    owner_epoch: int
    settled_position: int
    record_digest: str
    version: int = PROVENANCE_VERSION
    gateway_call_ordinal: int = 0

    def __post_init__(self) -> None:
        for name in ("child_id", "journal_identity", "run_id", "worker_id"):
            value = getattr(self, name)
            if (not isinstance(value, str) or not value.strip()
                    or len(value) > _MAX_PROVENANCE_TEXT):
                raise InvalidSubagentRequest(f"checkpoint provenance {name} must be bounded text")
        if self.cursor is not None and not isinstance(self.cursor, str):
            raise InvalidSubagentRequest("checkpoint provenance cursor must be text")
        for name, minimum in (("sequence", 0), ("owner_epoch", 1), ("settled_position", 0),
                              ("version", 1), ("gateway_call_ordinal", 0)):
            value = getattr(self, name)
            if type(value) is not int or value < minimum:
                raise InvalidSubagentRequest(f"checkpoint provenance {name} is invalid")
        for name in ("state_digest", "record_digest"):
            value = getattr(self, name)
            if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
                raise InvalidSubagentRequest(f"checkpoint provenance {name} must be SHA-256 hex")

    @staticmethod
    def compute_digest(*, child_id: str, sequence: int, state_digest: str,
                       cursor: str | None, journal_identity: str, run_id: str,
                       worker_id: str, owner_epoch: int, settled_position: int,
                       version: int = PROVENANCE_VERSION,
                       gateway_call_ordinal: int = 0) -> str:
        fields: dict[str, object] = {
            "child_id": child_id, "sequence": sequence, "state_digest": state_digest,
            "cursor": cursor, "journal_identity": journal_identity, "run_id": run_id,
            "worker_id": worker_id, "owner_epoch": owner_epoch,
            "settled_position": settled_position, "version": version,
        }
        if version >= _GATEWAY_ORDINAL_VERSION:
            fields["gateway_call_ordinal"] = gateway_call_ordinal
        return hashlib.sha256(_canonical_bytes(fields)).hexdigest()

    @classmethod
    def stamp(cls, *, child_id: str, sequence: int, state_digest: str,
              cursor: str | None, journal_identity: str, run_id: str,
              worker_id: str, owner_epoch: int, settled_position: int,
              gateway_call_ordinal: int = 0) -> CheckpointProvenance:
        fields = {
            "child_id": child_id, "sequence": sequence, "state_digest": state_digest,
            "cursor": cursor, "journal_identity": journal_identity, "run_id": run_id,
            "worker_id": worker_id, "owner_epoch": owner_epoch,
            "settled_position": settled_position,
            "gateway_call_ordinal": gateway_call_ordinal,
        }
        return cls(**fields, record_digest=cls.compute_digest(**fields))

    @property
    def digest_valid(self) -> bool:
        if self.version < _GATEWAY_ORDINAL_VERSION and self.gateway_call_ordinal != 0:
            # A version-1 digest does not cover the ordinal, so a nonzero
            # ordinal on such a record cannot have been stamped.
            return False
        return self.record_digest == self.compute_digest(
            child_id=self.child_id, sequence=self.sequence,
            state_digest=self.state_digest, cursor=self.cursor,
            journal_identity=self.journal_identity, run_id=self.run_id,
            worker_id=self.worker_id, owner_epoch=self.owner_epoch,
            settled_position=self.settled_position, version=self.version,
            gateway_call_ordinal=self.gateway_call_ordinal,
        )


@dataclass(frozen=True)
class ContinuableCheckpoint:
    """An immutable, monotonic child state snapshot.

    ``provenance`` is ``None`` for checkpoints written without a host
    provenance hook and for rows written before provenance existed.  Such a
    checkpoint is *provenance-absent*: it remains readable, but it can never
    authorize resume-from-checkpoint against the effect journal.
    """

    child_id: str
    sequence: int
    state: Mapping[str, Any] = field(default_factory=dict)
    cursor: str | None = None
    provenance: CheckpointProvenance | None = None

    def __post_init__(self) -> None:
        if not self.child_id.strip() or self.sequence < 0:
            raise InvalidSubagentRequest("checkpoint child_id and non-negative sequence are required")
        if not isinstance(self.state, Mapping):
            raise InvalidSubagentRequest("checkpoint state must be a mapping")
        if self.provenance is not None and not isinstance(self.provenance, CheckpointProvenance):
            raise InvalidSubagentRequest("checkpoint provenance must be host-stamped provenance")
        object.__setattr__(self, "state", deepcopy(dict(self.state)))

    @property
    def provenance_absent(self) -> bool:
        return self.provenance is None


def provenance_subject_error(checkpoint: ContinuableCheckpoint) -> str | None:
    """Return why a present provenance record does not describe ``checkpoint``.

    Stores call this inside their compare-and-set so a provenance record can
    only ever be persisted next to the exact checkpoint it was stamped for.
    Absent provenance is not an error here; it is refused at resume time.
    """
    provenance = checkpoint.provenance
    if provenance is None:
        return None
    if (provenance.child_id, provenance.sequence, provenance.cursor) != (
        checkpoint.child_id, checkpoint.sequence, checkpoint.cursor,
    ):
        return "checkpoint provenance subject does not match the checkpoint"
    try:
        digest = checkpoint_state_digest(checkpoint.state)
    except InvalidSubagentRequest:
        return "checkpoint provenance cannot bind a non-canonical state"
    if provenance.state_digest != digest:
        return "checkpoint provenance state digest does not match the checkpoint"
    if not provenance.digest_valid:
        return "checkpoint provenance record digest is invalid"
    return None


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
        request = SubagentRequest(request.parent_id, request.prompt, request.budget, child_id, request.metadata, request.resume_key, request.idempotency_key)
        record = self._repository.create(ContinuableRecord(request))
        control = _Cancellation()
        with self._lock:
            self._controls[child_id] = control
        self._launch(record, context, runner, control)
        return _Handle(self, child_id, request.parent_id)

    def _launch(self, record: ContinuableRecord, context: OperationContext, runner: Runner, control: _Cancellation) -> None:
        self._repository.update(record.request.child_id, status=SubagentStatus.RUNNING, recovery_required=False)
        thread = owned_runtime_thread(target=self._run, args=(record.request.child_id, context, runner, control), daemon=True)
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
    "CheckpointProvenance", "PROVENANCE_VERSION", "checkpoint_state_digest",
    "provenance_subject_error", "ContinuableCheckpoint", "ContinuableRecord", "ContinuableSubagentRepository",
    "ContinuableSubagentService", "InMemoryContinuableSubagentRepository", "CheckpointWriter", "Runner",
    "ContinuableSubagentState", "SubagentCheckpoint", "SubagentStore", "ResumeToken",
]
