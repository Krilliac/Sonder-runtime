"""Structured delegation integration over the existing child-agent port."""
from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)

from sonder_runtime.application.agents.lineage_delegation import (
    DelegationRequest,
    DelegationStatus,
    IntegrationError,
    ResultEvidence,
    delegation_digest,
)
from sonder_runtime.application.context import OperationContext
from sonder_runtime.application.ports.continuation_mutations import (
    ContinuationStorageFailure,
)
from sonder_runtime.application.ports.event_sink import EventSink
from sonder_runtime.application.ports.subagents import (
    SubagentBudget,
    SubagentHandle,
    SubagentProvider,
    SubagentRequest,
    SubagentResult,
)
from sonder_runtime.application.ports.worker_registry import (
    WorkerLaunch,
    WorkerRecord,
    WorkerRegistry,
    WorkerStatus,
)


@runtime_checkable
class TerminalChildWorkerRegistry(WorkerRegistry, Protocol):
    """Registry able to prove one delegated result against durable child state."""

    def terminal_result(self, worker_id: str) -> SubagentResult | None: ...

    def record_verification(
        self, worker_id: str, verification: Mapping[str, object], *, expected_revision: int,
    ) -> WorkerRecord | None: ...


@runtime_checkable
class ReservationProvenanceWorkerRegistry(WorkerRegistry, Protocol):
    """Distinguish a new reservation from an idempotently returned one."""

    def admit_with_creation(self, launch: WorkerLaunch) -> tuple[WorkerRecord, bool]: ...


@runtime_checkable
class UnstartedCancellationProvider(SubagentProvider, Protocol):
    """Cancel only an unchanged reservation that has never started."""

    def cancel_unstarted(
        self, child_id: str, *, expected_revision: int, reason: str = "cancellation requested"
    ) -> bool: ...


@dataclass(frozen=True)
class DelegatedResult:
    """Provider result plus the bounded evidence envelope for callers."""

    request_digest: str
    result: SubagentResult
    evidence: ResultEvidence


class DelegationService:
    """Translate validated agent envelopes to and from ``SubagentProvider``."""

    def __init__(
        self,
        provider: SubagentProvider,
        event_sink: EventSink | None = None,
        worker_registry: WorkerRegistry | None = None,
    ) -> None:
        self._provider = provider
        self._events = event_sink
        self._worker_registry = worker_registry

    def dispatch(self, request: DelegationRequest, context: OperationContext) -> SubagentHandle:
        """Spawn one child only when its assignment fits the parent context."""
        logger.debug(f"DelegationService.dispatch: delegation_id={request.delegation_id!r}, preset={request.preset.name!r}, role={request.preset.role.value!r}")
        assignment = request.workspace.guard()
        if request.execution_contract.requested and self._worker_registry is None:
            raise IntegrationError(
                "execution contract requires a durable worker registry"
            )
        for owned in request.execution_contract.owned_files:
            # Owned files are exclusive mutation targets, so each must be an
            # absolute path the child's write assignment actually permits.
            if not Path(owned).is_absolute():
                raise IntegrationError("owned files must be absolute paths inside a write root")
            if not assignment.permits(owned, write=True):
                raise IntegrationError(f"owned file is outside the write assignment: {owned}")
        if context.workspace_roots:
            parent_roots = tuple(root.resolve(strict=False) for root in context.workspace_roots)
            if not all(any(_inside(Path(root).resolve(strict=False), parent) for parent in parent_roots)
                       for root in request.workspace.read_roots):
                raise IntegrationError("delegated workspace is outside the parent context")
        budget = request.preset.budget.limit
        child_request = SubagentRequest(
            parent_id=request.lineage.parent_id,
            child_id=request.lineage.child_id,
            prompt=request.prompt,
            budget=SubagentBudget(
                max_steps=budget.steps,
                max_output_tokens=budget.output_tokens,
                max_wall_seconds=budget.wall_seconds,
            ),
            metadata=(
                ("delegation_id", request.delegation_id),
                ("request_digest", delegation_digest(request)),
                ("preset", request.preset.name),
                ("role", request.preset.role.value),
                ("workspace_read_roots", "|".join(request.workspace.read_roots)),
                ("workspace_write_roots", "|".join(request.workspace.write_roots)),
            ),
            # delegation_id is the durable task identity; the repository
            # scopes these keys by parent_id before rejecting active duplicates.
            resume_key=request.delegation_id,
            idempotency_key=request.delegation_id,
        )
        admitted: WorkerRecord | None = None
        created_reservation = False
        if self._worker_registry is not None:
            # The continuation-backed registry performs the durable duplicate
            # check before provider threads are created.  The provider then
            # consumes that same reservation; no parallel active-worker store
            # is opened.
            reservation_metadata = child_request.metadata + (
                ("worker_registry_admitted", "true"),
                ("worker_role", request.preset.role.value),
                ("model", "local-provider"),
                ("backend", "subagent-provider"),
                ("effort", "default"),
                ("scope", "|".join(request.workspace.read_roots + request.workspace.write_roots)),
                ("allowed_tools", "|".join(request.preset.capabilities)),
                ("owner_id", context.principal_id),
                ("worker_id", child_request.child_id or request.delegation_id),
                ("context_workspace_roots", "|".join(map(str, context.workspace_roots))),
                ("context_cloud_allowed", str(context.cloud_allowed)),
                ("context_remote_ollama_allowed", str(context.remote_ollama_allowed)),
                ("context_session_id", str(context.session_id)),
                ("retry_max_attempts", "1"),
            )
            owner_nonce = getattr(self._worker_registry, "owner_nonce", "")
            if owner_nonce:
                reservation_metadata += (("owner_nonce", owner_nonce),)
                reservation_metadata += (("owner_pid", str(getattr(self._worker_registry, "owner_pid", ""))),)
                reservation_metadata += (("owner_host", str(getattr(self._worker_registry, "owner_host", ""))),)
            worker_launch = WorkerLaunch(
                worker_id=child_request.child_id or request.delegation_id,
                parent_id=child_request.parent_id,
                role=request.preset.role.value,
                model="local-provider",
                backend="subagent-provider",
                effort="default",
                scope=tuple(request.workspace.read_roots + request.workspace.write_roots),
                allowed_tools=tuple(request.preset.capabilities),
                budgets={
                    "max_steps": budget.steps,
                    "max_output_tokens": budget.output_tokens,
                    "max_wall_seconds": budget.wall_seconds,
                },
                retry_policy={"max_attempts": 1},
                resume_key=request.delegation_id,
                idempotency_key=request.delegation_id,
                prompt=request.prompt,
                owner_id=context.principal_id,
                metadata=reservation_metadata,
                execution_contract=request.execution_contract,
            )
            # The provider receives the exact metadata retained by the
            # continuation reservation, so the CAS admission cannot be
            # confused with a caller that merely reused the same key.
            if isinstance(self._worker_registry, ReservationProvenanceWorkerRegistry):
                admitted, created_reservation = self._worker_registry.admit_with_creation(worker_launch)
            else:
                admitted = self._worker_registry.admit(worker_launch)
            canonical_launch = getattr(admitted, "launch", worker_launch)
            child_request = SubagentRequest(
                child_request.parent_id, child_request.prompt, child_request.budget,
                canonical_launch.worker_id, canonical_launch.metadata,
                child_request.resume_key, child_request.idempotency_key,
            )
        logger.debug(f"DelegationService.dispatch: spawning child_id={request.lineage.child_id!r}, parent_id={request.lineage.parent_id!r}")
        try:
            handle = self._provider.spawn(child_request, context)
        except Exception as error:
            if (
                created_reservation
                and admitted is not None
                and admitted.status is WorkerStatus.QUEUED
                and isinstance(self._provider, UnstartedCancellationProvider)
                and not isinstance(error, ContinuationStorageFailure)
            ):
                # A factory or adapter can fail after admission but before
                # spawn. The revision/state fence protects a runner that
                # already started; provenance protects another caller's
                # idempotently returned reservation.
                self._provider.cancel_unstarted(
                    admitted.launch.worker_id,
                    expected_revision=admitted.revision,
                    reason="provider launch failed",
                )
            raise
        logger.info(f"agent delegated: delegation_id={request.delegation_id!r}, preset={request.preset.name!r}, role={request.preset.role.value!r}, child_id={handle.child_id!r}")
        logger.debug(f"DelegationService.dispatch: child spawned, handle.child_id={handle.child_id!r}")
        if self._events:
            self._events.emit(
                "agent.delegation.accepted",
                summary="delegated child accepted",
                detail={"delegation_id": request.delegation_id, "child_id": handle.child_id},
                correlation_id=context.correlation_id,
                operation_id=request.delegation_id,
            )
        return handle

    def integrate(
        self,
        request: DelegationRequest,
        result: SubagentResult,
        *,
        verification: Iterable[str] = (),
        verification_commands: Iterable[tuple[str, ...]] = (),
        artifacts: Iterable[str] = (),
    ) -> DelegatedResult:
        """Validate provider identity and publish a bounded structured result."""
        logger.debug(f"DelegationService.integrate: delegation_id={request.delegation_id!r}, result.status={result.status.value!r}, child_id={result.child_id!r}")
        if result.child_id != request.lineage.child_id or result.parent_id != request.lineage.parent_id:
            raise IntegrationError("provider result does not match delegation lineage")
        contract_requested = request.execution_contract.requested
        if contract_requested and self._worker_registry is None:
            raise IntegrationError(
                "execution contract requires a durable worker registry"
            )
        registry = self._worker_registry
        if registry is not None:
            if not isinstance(registry, TerminalChildWorkerRegistry):
                raise IntegrationError("durable worker registry cannot prove a terminal child result")
            authoritative = registry.terminal_result(result.child_id)
            if authoritative is None or authoritative != result:
                raise IntegrationError("result does not match the durable terminal result")
        succeeded = result.status.value == "succeeded"
        verification_values = tuple(verification)
        command_values = tuple(tuple(command) for command in verification_commands)
        if registry is not None:
            record = registry.get(result.child_id)
            if record is None:
                raise IntegrationError("worker registry record disappeared before execution gate")
            contract = record.launch.execution_contract
            if contract_requested and contract != request.execution_contract:
                raise IntegrationError("persisted worker execution contract does not match request")
            # Criteria and command proof certify success only.  A failed or
            # interrupted worker must still publish its failure evidence.
            if succeeded:
                missing = tuple(item for item in contract.success_criteria if item not in verification_values)
                if missing:
                    raise IntegrationError("worker execution criteria were not verified: " + ", ".join(missing))
                if contract.verification_commands != command_values:
                    raise IntegrationError("worker verification commands do not match its execution contract")
        if not succeeded:
            logger.error(f"delegation failed: delegation_id={request.delegation_id!r}, child_id={result.child_id!r}, status={result.status.value!r}")
            logger.warning(f"delegation failed: delegation_id={request.delegation_id!r}, child_id={result.child_id!r}, status={result.status.value!r}")
        output = result.output if succeeded else (result.error.message if result.error else "failed")
        if result.usage.steps and result.usage.steps > 50:
            logger.warning(f"delegation used high step count: delegation_id={request.delegation_id!r}, usage_steps={result.usage.steps}")
        evidence = ResultEvidence(
            request.delegation_id,
            DelegationStatus.SUCCEEDED if succeeded else DelegationStatus.FAILED,
            ResultEvidence.digest(output),
            verification_values,
            tuple(artifacts),
            None if succeeded else output,
            usage_steps=result.usage.steps,
        )
        if registry is not None:
            persisted = registry.record_verification(
                result.child_id,
                {
                    "status": evidence.status.value,
                    "terminal_status": result.status.value,
                    "output_digest": evidence.output_digest,
                    "usage_steps": result.usage.steps,
                    "usage": {
                        "steps": result.usage.steps,
                        "output_tokens": result.usage.output_tokens,
                        "wall_seconds": result.usage.wall_seconds,
                    },
                    "error_code": result.error.code if result.error else "",
                    "error_message": result.error.message if result.error else "",
                    "verification": evidence.verification,
                    "success_criteria": record.launch.execution_contract.success_criteria,
                    "verification_commands": record.launch.execution_contract.verification_commands,
                    "context_policy": record.launch.execution_contract.context_policy.value,
                    "context_inputs": tuple(
                        (item.reference, item.sha256)
                        for item in record.launch.execution_contract.context_inputs
                    ),
                    "inherited_context_sha256": record.launch.execution_contract.inherited_context_sha256,
                    "owned_files": record.launch.execution_contract.owned_files,
                    "task_scope": record.launch.execution_contract.task_scope,
                    "artifacts": evidence.artifacts,
                },
                expected_revision=record.revision,
            )
            if persisted is None:
                raise IntegrationError("worker registry verification compare-and-set failed")
        logger.info(f"delegation integrated: delegation_id={request.delegation_id!r}, status={evidence.status.value!r}, child_id={result.child_id!r}, usage_steps={evidence.usage_steps}")
        logger.debug(f"DelegationService.integrate: evidence_status={evidence.status.value!r}, usage_steps={evidence.usage_steps}")
        return DelegatedResult(delegation_digest(request), result, evidence)


__all__ = ["DelegatedResult", "DelegationService"]


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True
