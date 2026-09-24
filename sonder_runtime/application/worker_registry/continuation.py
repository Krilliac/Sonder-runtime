"""Worker-registry projection over the durable child-session authority.

The worker registry must not create a second active-worker database.  This
adapter uses ``durable_child_session`` for admission, identity, lifecycle and
CAS revisions, while exposing the smaller worker contract to callers that
need role/model/scope metadata.  A reservation is marked in request metadata
and is consumed by ``DurableContinuationService.spawn`` before a runner is
started; a different owner or launch cannot claim it.
"""
from __future__ import annotations

from collections.abc import Mapping
import json
import os
import platform
import threading

from sonder_runtime.application.ports.continuation_records import (
    ChildSessionLineage,
    DurableChildSession,
)
from sonder_runtime.application.ports.subagents import (
    InvalidSubagentRequest,
    SubagentBudget,
    SubagentError,
    SubagentRequest,
    SubagentResult,
    SubagentStatus,
    SubagentUsage,
)
from sonder_runtime.application.ports.worker_registry import (
    DuplicateWorkerError,
    WorkerLaunch,
    WorkerRecord,
    WorkerRegistry,
    WorkerRegistryError,
    WorkerContextInput,
    WorkerContextPolicy,
    WorkerExecutionContract,
    WorkerStatus,
)
from sonder_runtime.application.subagents.durable_continuation import (
    DurableContinuationRepository,
)
from sonder_runtime.application.owner_process import recorded_owner_is_dead


_RESERVATION_MARKER = "worker_registry_admitted"
_ADMISSION_LOCK = threading.Lock()


def _metadata(request: SubagentRequest) -> dict[str, str]:
    values = dict(request.metadata)
    if len(values) != len(request.metadata):
        raise WorkerRegistryError("child metadata keys must be unique")
    return values


def _budget_values(budget: SubagentBudget) -> dict[str, object]:
    return {
        name: getattr(budget, name)
        for name in (
            "max_children", "max_depth", "max_concurrency", "max_steps",
            "max_wall_seconds", "max_output_tokens",
        )
        if getattr(budget, name) is not None
    }


def _budget(launch: WorkerLaunch) -> SubagentBudget:
    values = dict(launch.budgets)
    aliases = {"steps": "max_steps", "output_tokens": "max_output_tokens", "wall_seconds": "max_wall_seconds"}
    values = {aliases.get(key, key): value for key, value in values.items()}
    allowed = {
        name: values[name]
        for name in (
            "max_children", "max_depth", "max_concurrency", "max_steps",
            "max_wall_seconds", "max_output_tokens",
        )
        if name in values and values[name] is not None
    }
    if not allowed:
        raise WorkerRegistryError("worker launch requires at least one budget ceiling")
    try:
        return SubagentBudget(**allowed)
    except (TypeError, ValueError) as exc:
        raise WorkerRegistryError("worker launch budget is invalid") from exc


_CONTRACT_KEYS = frozenset((
    "execution_success_criteria",
    "execution_verification_commands",
    "execution_context_policy",
    "execution_context_inputs",
    "execution_inherited_context_sha256",
    "execution_owned_files",
    "execution_task_scope",
    "execution_speculative_lane",
))


def _compact(value: object) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def _contract_metadata(contract: WorkerExecutionContract) -> tuple[tuple[str, str], ...]:
    """Serialize a requested contract into canonical child-session metadata."""
    if not contract.requested:
        return ()
    return (
        ("execution_success_criteria", _compact(contract.success_criteria)),
        ("execution_verification_commands", _compact(contract.verification_commands)),
        ("execution_context_policy", contract.context_policy.value),
        ("execution_context_inputs", _compact([[item.reference, item.sha256] for item in contract.context_inputs])),
        ("execution_inherited_context_sha256", contract.inherited_context_sha256),
        ("execution_owned_files", _compact(contract.owned_files)),
        ("execution_task_scope", contract.task_scope),
        ("execution_speculative_lane", "true" if contract.speculative_lane else "false"),
    )


def _string_list(raw: object) -> list[str]:
    if type(raw) is not list or any(type(item) is not str for item in raw):
        raise ValueError("execution contract JSON shape is invalid")
    return raw


def _contract_from_metadata(metadata: Mapping[str, str]) -> WorkerExecutionContract:
    """Rebuild a persisted contract; any malformed field fails closed."""
    try:
        criteria = _string_list(json.loads(metadata.get("execution_success_criteria", "[]")))
        raw_commands = json.loads(metadata.get("execution_verification_commands", "[]"))
        if type(raw_commands) is not list or any(not _string_list(command) for command in raw_commands):
            raise ValueError("execution contract JSON shape is invalid")
        raw_inputs = json.loads(metadata.get("execution_context_inputs", "[]"))
        if type(raw_inputs) is not list or any(len(_string_list(item)) != 2 for item in raw_inputs):
            raise ValueError("execution context inputs JSON shape is invalid")
        owned = _string_list(json.loads(metadata.get("execution_owned_files", "[]")))
        speculative = metadata.get("execution_speculative_lane", "false")
        if speculative not in {"true", "false"}:
            raise ValueError("execution speculative lane flag is invalid")
        return WorkerExecutionContract(
            success_criteria=tuple(criteria),
            verification_commands=tuple(tuple(command) for command in raw_commands),
            context_policy=WorkerContextPolicy(
                metadata.get("execution_context_policy", WorkerContextPolicy.UNSPECIFIED.value)
            ),
            context_inputs=tuple(WorkerContextInput(reference, digest) for reference, digest in raw_inputs),
            inherited_context_sha256=metadata.get("execution_inherited_context_sha256", ""),
            owned_files=tuple(owned),
            task_scope=metadata.get("execution_task_scope", ""),
            speculative_lane=speculative == "true",
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise WorkerRegistryError("persisted worker execution contract is invalid") from exc


def _request_for(launch: WorkerLaunch) -> SubagentRequest:
    if not launch.prompt.strip():
        raise WorkerRegistryError("composed worker launch requires a prompt")
    if not launch.owner_id.strip():
        raise WorkerRegistryError("composed worker launch requires an owner")
    contract_metadata = _contract_metadata(launch.execution_contract)
    caller_metadata = tuple(item for item in launch.metadata if item[0] not in _CONTRACT_KEYS)
    metadata = (caller_metadata + contract_metadata) if launch.metadata else (
        (_RESERVATION_MARKER, "true"),
        ("worker_role", launch.role),
        ("model", launch.model),
        ("backend", launch.backend),
        ("effort", launch.effort),
        ("scope", "|".join(launch.scope)),
        ("allowed_tools", "|".join(launch.allowed_tools)),
        ("owner_id", launch.owner_id),
        ("worker_id", launch.worker_id),
        ("retry_max_attempts", str(launch.retry_policy.get("max_attempts", 1))),
    ) + contract_metadata
    return SubagentRequest(
        parent_id=launch.parent_id,
        prompt=launch.prompt,
        budget=_budget(launch),
        child_id=launch.worker_id,
        metadata=metadata,
        resume_key=launch.resume_key,
        idempotency_key=launch.idempotency_key,
    )


def _status(value: SubagentStatus) -> WorkerStatus:
    return {
        SubagentStatus.CREATED: WorkerStatus.QUEUED,
        SubagentStatus.QUEUED: WorkerStatus.QUEUED,
        SubagentStatus.RUNNING: WorkerStatus.RUNNING,
        SubagentStatus.SUCCEEDED: WorkerStatus.SUCCEEDED,
        SubagentStatus.FAILED: WorkerStatus.FAILED,
        SubagentStatus.CANCELLED: WorkerStatus.INTERRUPTED,
        SubagentStatus.TIMED_OUT: WorkerStatus.INTERRUPTED,
    }[value]


class ContinuationWorkerRegistry(WorkerRegistry):
    """Expose admission/evidence over one durable child-session authority.

    ``DurableContinuationService`` remains the only owner of start, progress,
    retry, resume and terminal result transitions.  The lifecycle methods
    required by the legacy registry protocol therefore fail closed here; this
    adapter intentionally narrows that protocol to ``admit``, ``get`` and
    ``record_verification`` instead of maintaining a competing state machine.
    """

    def __init__(
        self,
        repository: DurableContinuationRepository,
        *,
        owner_nonce: str = "",
        owner_pid: int | None = None,
        owner_host: str = "",
    ) -> None:
        self._repository = repository
        self._owner_nonce = owner_nonce.strip()
        self._owner_pid = os.getpid() if owner_pid is None else owner_pid
        self._owner_host = owner_host or platform.node()

    @property
    def owner_nonce(self) -> str:
        return self._owner_nonce

    @property
    def owner_pid(self) -> int:
        return self._owner_pid

    @property
    def owner_host(self) -> str:
        return self._owner_host

    def admit(self, launch: WorkerLaunch) -> WorkerRecord:
        request = _request_for(launch)
        existing = self._repository.get(launch.worker_id)
        if existing is None:
            for key, namespace in ((launch.resume_key, "resume"), (launch.idempotency_key, "idempotency")):
                existing = self._repository.get_active_by_key(launch.parent_id, key, namespace)
                if existing is not None:
                    break
        if existing is None:
            lookup = getattr(self._repository, "get_by_key", None)
            if callable(lookup):
                for key, namespace in ((launch.resume_key, "resume"), (launch.idempotency_key, "idempotency")):
                    if key:
                        existing = lookup(launch.parent_id, key, namespace)
                        if existing is not None:
                            break
        if existing is None and self._owner_nonce and dict(request.metadata).get("owner_nonce") != self._owner_nonce:
            raise WorkerRegistryError("worker launch owner nonce does not match this provider")
        if existing is not None:
            record = self._project(existing)
            if record.launch != launch:
                current_metadata = dict(record.launch.metadata)
                requested_metadata = dict(launch.metadata)
                stable_scope_match = (
                    record.launch.parent_id == launch.parent_id
                    and record.launch.role == launch.role
                    and record.launch.model == launch.model
                    and record.launch.backend == launch.backend
                    and record.launch.effort == launch.effort
                    and record.launch.scope == launch.scope
                    and record.launch.allowed_tools == launch.allowed_tools
                    and record.launch.budgets == launch.budgets
                    and record.launch.retry_policy == launch.retry_policy
                    and record.launch.resume_key == launch.resume_key
                    and record.launch.idempotency_key == launch.idempotency_key
                    and record.launch.prompt == launch.prompt
                    and record.launch.owner_id == launch.owner_id
                    and record.launch.execution_contract == launch.execution_contract
                    and all(
                        current_metadata.get(key) == requested_metadata.get(key)
                        for key in set(current_metadata) | set(requested_metadata)
                        if key not in {
                            "owner_nonce", "owner_pid", "owner_host", "worker_id", "request_digest",
                        } | _CONTRACT_KEYS
                    )
                )
                owner_only_difference = stable_scope_match
                owner_dead = (
                    owner_only_difference
                    and current_metadata.get("owner_nonce") != self._owner_nonce
                    and recorded_owner_is_dead(current_metadata)
                )
                if owner_dead:
                    # Return the persisted metadata, including its original
                    # nonce, so the provider can consume it after independently
                    # proving the old owner process is gone.
                    return record
                if record.status in {WorkerStatus.QUEUED, WorkerStatus.RUNNING}:
                    raise DuplicateWorkerError("active worker identity or scope already belongs to another launch")
                if stable_scope_match and record.status in {
                    WorkerStatus.SUCCEEDED, WorkerStatus.FAILED, WorkerStatus.INTERRUPTED,
                }:
                    # Stable keys identify the durable child; a retry may carry
                    # a fresh proposal child ID, but the provider must consume
                    # the canonical persisted worker identity.
                    return record
                raise WorkerRegistryError("worker identity is already bound to a different terminal launch")
            return record

        parent = self._repository.get(launch.parent_id)
        if parent is None:
            raise WorkerRegistryError("worker parent is not durably registered")
        parent_is_root = parent is not None and _metadata(parent.request).get("provider_root") == "true"
        lineage = ChildSessionLineage(
            launch.parent_id,
            () if parent_is_root else (parent.lineage.chain if parent else ()),
        )
        # Owned-file/task exclusivity is checked and the reservation created
        # under one process-wide lock.  Stable-key uniqueness remains the
        # repository's atomic SQLite check; ownership exclusivity is serialized
        # only within this process (see ISSUE-510 worker contract evidence).
        with _ADMISSION_LOCK:
            self._reject_ownership_conflict(launch)
            try:
                created = self._repository.create(DurableChildSession(request, lineage))
            except InvalidSubagentRequest as exc:
                # The repository performs the atomic key check.  Re-read only to
                # classify the conflict; never retry the create.
                active = self._repository.get_active_by_key(launch.parent_id, launch.resume_key, "resume")
                if active is not None:
                    raise DuplicateWorkerError("active worker resume key already exists") from exc
                raise WorkerRegistryError("worker admission was rejected by durable child storage") from exc
        return self._project(created)

    def _reject_ownership_conflict(self, launch: WorkerLaunch) -> None:
        """Refuse a second active worker for the same owned files or task."""
        contract = launch.execution_contract
        if not contract.owned_files and not contract.task_scope:
            return
        for session in self._repository.list_active():
            if session.request.child_id == launch.worker_id:
                continue
            reason = contract.conflicts_with(_contract_from_metadata(_metadata(session.request)))
            if reason:
                raise DuplicateWorkerError(reason)

    def get(self, worker_id: str) -> WorkerRecord | None:
        record = self._repository.get(worker_id)
        return None if record is None else self._project(record)

    def start(self, worker_id: str, *, expected_revision: int) -> WorkerRecord | None:
        raise WorkerRegistryError(
            "worker lifecycle is owned by DurableContinuationService; use spawn or explicit resume"
        )

    def progress(self, worker_id: str, progress: Mapping[str, object], *, expected_revision: int) -> WorkerRecord | None:
        # Checkpoint state is owned by the continuation service.  Accepting a
        # second progress column here would split lifecycle authority.
        raise WorkerRegistryError("progress is recorded through durable continuation checkpoints")

    def finish(
        self,
        worker_id: str,
        *,
        status: WorkerStatus,
        verification: Mapping[str, object],
        error: str = "",
        expected_revision: int,
    ) -> WorkerRecord | None:
        raise WorkerRegistryError(
            "worker lifecycle is owned by DurableContinuationService; use integrate for verification"
        )

    def record_verification(
        self,
        worker_id: str,
        verification: Mapping[str, object],
        *,
        expected_revision: int,
    ) -> WorkerRecord | None:
        current = self._repository.get(worker_id)
        if current is None or current.revision != expected_revision or current.status not in {
            SubagentStatus.SUCCEEDED,
            SubagentStatus.FAILED,
            SubagentStatus.CANCELLED,
            SubagentStatus.TIMED_OUT,
        }:
            return None
        updated = self._repository.update(
            worker_id,
            status=current.status,
            expected_revision=expected_revision,
            usage=current.usage,
            verification=verification,
        )
        return None if updated is None else self._project(updated)

    @staticmethod
    def _project(session: DurableChildSession) -> WorkerRecord:
        metadata = _metadata(session.request)
        scope = tuple(filter(None, metadata.get("scope", "").split("|")))
        tools = tuple(filter(None, metadata.get("allowed_tools", "").split("|")))
        max_attempts = int(metadata.get("retry_max_attempts", "1"))
        execution_contract = _contract_from_metadata(metadata)
        launch = WorkerLaunch(
            worker_id=session.request.child_id or "",
            parent_id=session.request.parent_id,
            role=metadata.get("worker_role", metadata.get("role", "worker")),
            model=metadata.get("model", "unknown"),
            backend=metadata.get("backend", "durable-child"),
            effort=metadata.get("effort", "default"),
            scope=scope or ("durable-child",),
            allowed_tools=tools or ("continuation",),
            budgets=_budget_values(session.request.budget),
            retry_policy={"max_attempts": max_attempts},
            resume_key=session.request.resume_key,
            idempotency_key=session.request.idempotency_key,
            prompt=session.request.prompt,
            owner_id=metadata.get("owner_id", ""),
            metadata=tuple(session.request.metadata),
            execution_contract=execution_contract,
        )
        progress = {}
        if session.checkpoint is not None:
            progress = {"sequence": session.checkpoint.sequence, "cursor": session.checkpoint.cursor or ""}
        verification = dict(session.terminal_verification)
        if not verification and session.result is not None:
            verification = {"status": session.result.status.value, "usage": {"steps": session.result.usage.steps}}
        error = "" if session.result is None or session.result.error is None else session.result.error.message
        return WorkerRecord(
            launch,
            _status(session.status),
            progress,
            verification,
            error,
            session.revision,
            1 if session.status is not SubagentStatus.CREATED else 0,
        )


__all__ = ["ContinuationWorkerRegistry"]
