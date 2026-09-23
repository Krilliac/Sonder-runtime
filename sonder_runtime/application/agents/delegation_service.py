"""Structured delegation integration over the existing child-agent port."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)

from sonder_runtime.application.agents.lineage_delegation import (
    DelegationRequest, DelegationStatus, IntegrationError, ResultEvidence,
    delegation_digest,
)
from sonder_runtime.application.context import OperationContext
from sonder_runtime.application.ports.event_sink import EventSink
from sonder_runtime.application.ports.subagents import (
    SubagentBudget, SubagentHandle, SubagentProvider, SubagentRequest, SubagentResult,
)
from sonder_runtime.application.ports.worker_registry import WorkerLaunch, WorkerRegistry


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
            )
            # The provider receives the exact metadata retained by the
            # continuation reservation, so the CAS admission cannot be
            # confused with a caller that merely reused the same key.
            admitted = self._worker_registry.admit(worker_launch)
            canonical_launch = getattr(admitted, "launch", worker_launch)
            child_request = SubagentRequest(
                child_request.parent_id, child_request.prompt, child_request.budget,
                canonical_launch.worker_id, canonical_launch.metadata,
                child_request.resume_key, child_request.idempotency_key,
            )
        logger.debug(f"DelegationService.dispatch: spawning child_id={request.lineage.child_id!r}, parent_id={request.lineage.parent_id!r}")
        handle = self._provider.spawn(child_request, context)
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
        artifacts: Iterable[str] = (),
    ) -> DelegatedResult:
        """Validate provider identity and publish a bounded structured result."""
        logger.debug(f"DelegationService.integrate: delegation_id={request.delegation_id!r}, result.status={result.status.value!r}, child_id={result.child_id!r}")
        if result.child_id != request.lineage.child_id or result.parent_id != request.lineage.parent_id:
            raise IntegrationError("provider result does not match delegation lineage")
        succeeded = result.status.value == "succeeded"
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
            tuple(verification),
            tuple(artifacts),
            None if succeeded else output,
            usage_steps=result.usage.steps,
        )
        if self._worker_registry is not None:
            get_record = getattr(self._worker_registry, "get", None)
            record_verification = getattr(self._worker_registry, "record_verification", None)
            if callable(get_record) and callable(record_verification):
                record = get_record(result.child_id)
                if record is None:
                    raise IntegrationError("worker registry record disappeared before verification")
                persisted = record_verification(
                    result.child_id,
                    {
                        "status": evidence.status.value,
                        "output_digest": evidence.output_digest,
                        "verification": evidence.verification,
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
