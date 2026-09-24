"""Structured delegation integration over the existing child-agent port."""
from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Protocol, runtime_checkable

from sonder_runtime.application.agents.lineage_delegation import (
    DelegationRequest,
    DelegationStatus,
    IntegrationError,
    LineageRecord,
    ResultEvidence,
    delegation_digest,
)
from sonder_runtime.application.agents.presets import resolve_preset
from sonder_runtime.application.agents.recursive_delegation import (
    DescendantDispatch,
    HypothesisDecision,
    NeedsDelegation,
    PartialDelegationError,
    StructuredChildResult,
)
from sonder_runtime.application.artifacts.readiness import (
    ArtifactReadiness,
    ArtifactReadinessBarrier,
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
    SubagentStatus,
    validate_child_budget,
)
from sonder_runtime.application.ports.worker_registry import (
    WorkerLaunch,
    WorkerRecord,
    WorkerRegistry,
    WorkerStatus,
)

logger = logging.getLogger(__name__)


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
        *,
        host_root_budget: SubagentBudget | None = None,
        register_host_root: Callable[[str, SubagentBudget, str], object] | None = None,
    ) -> None:
        if (host_root_budget is None) != (register_host_root is None):
            raise ValueError("host delegation root budget and registrar must be configured together")
        self._provider = provider
        self._events = event_sink
        self._worker_registry = worker_registry
        self._host_root_budget = host_root_budget
        self._register_host_root = register_host_root

    @staticmethod
    def root_id_for_context(context: OperationContext) -> str:
        """Stable host operation identity; one correlation cannot mint more roots."""
        if not context.correlation_id or not context.principal_id:
            raise IntegrationError("delegation requires a host operation and principal")
        identity = json.dumps((context.principal_id, context.correlation_id), separators=(",", ":"))
        return "delegation-root:" + hashlib.sha256(identity.encode()).hexdigest()

    def _ensure_host_root(self, request: DelegationRequest, context: OperationContext) -> None:
        if self._host_root_budget is None:
            return
        registry = self._worker_registry
        if registry is None or self._register_host_root is None:
            raise IntegrationError("host delegation requires a durable root registry")
        root_id = self.root_id_for_context(context)
        if request.lineage.root_id != root_id:
            raise IntegrationError("delegation root differs from the host operation identity")
        if request.lineage.depth == 1:
            if request.lineage.parent_id != root_id:
                raise IntegrationError("first-level delegation must use its operation root")
            self._register_host_root(root_id, self._host_root_budget, context.principal_id)
        root = registry.get(root_id)
        if (
            root is None or root.launch.parent_id != root_id
            or dict(root.launch.metadata).get("provider_root") != "true"
            or root.launch.budgets != {name: value for name, value in asdict(self._host_root_budget).items()
                                       if value is not None}
            or root.launch.owner_id != context.principal_id
        ):
            raise IntegrationError("delegation root differs from its immutable host grant")

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
        requested_budget = request.resource_budget or SubagentBudget(
            max_steps=budget.steps,
            max_output_tokens=budget.output_tokens,
            max_wall_seconds=budget.wall_seconds,
        )
        if request.resource_budget is not None and any(
            value is None or ceiling is not None and value > ceiling
            for value, ceiling in (
                (requested_budget.max_steps, budget.steps),
                (requested_budget.max_output_tokens, budget.output_tokens),
                (requested_budget.max_wall_seconds, budget.wall_seconds),
            )
        ):
            raise IntegrationError("delegated resource budget exceeds its role preset")
        self._ensure_host_root(request, context)
        if self._worker_registry is not None:
            registry_get = getattr(self._worker_registry, "get", None)
            parent = registry_get(request.lineage.parent_id) if callable(registry_get) else None
            if parent is not None:
                root, parent_depth = self._recorded_parent_lineage(
                    self._worker_registry, request.lineage.parent_id,
                )
                if (
                    request.lineage.root_id != root
                    or request.lineage.depth != parent_depth + 1
                ):
                    raise IntegrationError("delegation lineage differs from its durable parent")
                inherited = dict(parent.launch.budgets)
                for field in ("max_children", "max_depth", "max_concurrency"):
                    limit = inherited.get(field)
                    if limit is not None and getattr(requested_budget, field) is None:
                        requested_budget = replace(requested_budget, **{field: limit})
                # Keep the caller-facing typed budget error; the repository
                # independently repeats this under its atomic writer lock.
                validate_child_budget(requested_budget, SubagentBudget(**inherited))
        child_request = SubagentRequest(
            parent_id=request.lineage.parent_id,
            child_id=request.lineage.child_id,
            prompt=request.prompt,
            budget=requested_budget,
            metadata=(
                ("delegation_id", request.delegation_id),
                ("request_digest", delegation_digest(request)),
                ("preset", request.preset.name),
                ("role", request.preset.role.value),
                ("workspace_read_roots", "|".join(request.workspace.read_roots)),
                ("workspace_write_roots", "|".join(request.workspace.write_roots)),
                *((("hypothesis_digest", request.hypothesis_digest),
                    ("speculative_lane_id", request.speculative_lane_id))
                  if request.hypothesis_digest else ()),
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
                budgets={name: value for name, value in asdict(requested_budget).items()
                         if value is not None},
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

    @staticmethod
    def _recorded_parent_lineage(registry: WorkerRegistry, parent_id: str) -> tuple[str, int]:
        """Derive root/depth from authoritative parents, not proposed labels."""
        depth = 0
        seen: set[str] = set()
        cursor = parent_id
        while cursor not in seen and depth <= 8:
            seen.add(cursor)
            record = registry.get(cursor)
            if record is None:
                break
            if (
                record.launch.worker_id == record.launch.parent_id
                and dict(record.launch.metadata).get("provider_root") == "true"
            ):
                return cursor, depth
            depth += 1
            cursor = record.launch.parent_id
        raise IntegrationError("durable delegation parent has no registered root lineage")

    def dispatch_proposal(
        self,
        parent_request: DelegationRequest,
        parent_result: SubagentResult,
        proposal: NeedsDelegation,
        *,
        context: OperationContext,
    ) -> tuple[DescendantDispatch, ...]:
        """Admit a child's proposed specialists under its durable parent.

        The parent must first have passed ``integrate``. All descendants go
        through ordinary registry admission and provider execution; no child
        text can directly spawn a worker or grant itself fresh resources.
        """
        registry = self._worker_registry
        if not isinstance(proposal, NeedsDelegation) or not isinstance(registry, TerminalChildWorkerRegistry):
            raise IntegrationError("recursive delegation requires a typed proposal and durable registry")
        parent_id = parent_request.lineage.child_id
        if (
            proposal.parent_child_id != parent_id or parent_result.child_id != parent_id
            or parent_result.parent_id != parent_request.lineage.parent_id
            or parent_result.status is not SubagentStatus.SUCCEEDED
            or registry.terminal_result(parent_id) != parent_result
            or proposal.source_output_digest != ResultEvidence.digest(parent_result.output)
        ):
            raise IntegrationError("proposal is not bound to the durable successful parent result")
        parent = registry.get(parent_id)
        root_id = parent_request.lineage.root_id
        root = registry.get(root_id)
        if parent is None or root is None:
            raise IntegrationError("recursive parent and root must be durably registered")
        if self._recorded_parent_lineage(registry, parent_id) != (
            root_id, parent_request.lineage.depth,
        ):
            raise IntegrationError("recursive proposal lineage differs from the durable child")
        proof = parent.terminal_verification
        if (
            parent.launch.parent_id != parent_result.parent_id
            or dict(parent.launch.metadata).get("request_digest") != delegation_digest(parent_request)
            or proof.get("status") != "succeeded"
            or proof.get("output_digest") != proposal.source_output_digest
            or root.launch.worker_id != root_id
            or root.launch.parent_id != root_id
            or dict(root.launch.metadata).get("provider_root") != "true"
        ):
            raise IntegrationError("recursive parent lacks its integrated durable authority")
        root_limits = dict(root.launch.budgets)
        parent_limits = dict(parent.launch.budgets)
        for name in ("max_children", "max_depth", "max_concurrency"):
            if root_limits.get(name) is None or parent_limits.get(name) is None:
                raise IntegrationError("recursive admission requires finite durable fanout and depth ceilings")
        if root_limits["max_children"] > 8 or root_limits["max_concurrency"] > 8:
            raise IntegrationError("recursive root exceeds the conservative fanout ceiling")
        next_depth = parent_request.lineage.depth + 1
        if (
            next_depth > 2 or next_depth > root_limits["max_depth"]
            or next_depth > parent_limits["max_depth"]
            or len(proposal.specialists) > min(3, root_limits["max_children"], parent_limits["max_children"])
        ):
            raise IntegrationError("recursive depth or direct child budget exhausted")
        if context.expired or context.cancellation.cancelled:
            raise IntegrationError("recursive operation context is no longer active")
        parent_budget = SubagentBudget(**parent_limits)
        if any(getattr(parent_budget, key) is None for key in (
            "max_steps", "max_output_tokens", "max_wall_seconds",
        )):
            raise IntegrationError("recursive parent requires finite resource ceilings")
        remaining = {
            "max_steps": parent_budget.max_steps - parent_result.usage.steps,
            "max_output_tokens": parent_budget.max_output_tokens - (
                parent_result.usage.output_tokens
                if parent_result.usage.output_tokens is not None
                else len(parent_result.output.encode("utf-8"))
            ),
            "max_wall_seconds": parent_budget.max_wall_seconds - (parent_result.usage.wall_seconds or 0),
        }
        if any(value <= 0 for value in remaining.values()):
            raise IntegrationError("recursive parent has no remaining reserved resources")
        validated: list[DelegationRequest] = []
        for sequence, specialist in enumerate(proposal.specialists, 1):
            if any(not parent_request.workspace.permits(path) for path in specialist.workspace.read_roots) or any(
                not parent_request.workspace.permits(path, write=True) for path in specialist.workspace.write_roots
            ):
                raise IntegrationError("specialist workspace widens its parent grant")
            requested = specialist.budget
            if any(getattr(requested, key) is None or getattr(requested, key) > ceiling
                   for key, ceiling in remaining.items()):
                raise IntegrationError("specialist budget widens remaining parent reservation")
            bounds = {
                "max_children": min(2, root_limits["max_children"], parent_limits["max_children"]),
                "max_depth": min(2, root_limits["max_depth"], parent_limits["max_depth"]),
                "max_concurrency": min(3, root_limits["max_concurrency"], parent_limits["max_concurrency"]),
            }
            for name, bound in bounds.items():
                supplied = getattr(requested, name)
                if supplied is not None and supplied > bound:
                    raise IntegrationError(f"specialist {name} widens its recursive ceiling")
                if supplied is None:
                    requested = replace(requested, **{name: bound})
            validate_child_budget(requested, parent_budget)
            preset = resolve_preset(specialist.preset)
            lineage = LineageRecord(
                f"{proposal.proposal_id}:lineage:{sequence}", root_id, parent_id,
                specialist.child_id, next_depth, preset.name, preset.role,
                specialist.workspace, sequence=sequence,
            )
            validated.append(DelegationRequest(
                f"{proposal.proposal_id}:delegation:{sequence}", lineage,
                specialist.prompt, preset, specialist.workspace,
                ("recursive-specialist",), specialist.contract, requested,
                specialist.hypothesis_digest, specialist.speculative_lane_id,
            ))
        launched: list[DescendantDispatch] = []
        for child in validated:
            try:
                handle = self.dispatch(child, context)
            except Exception as error:
                if launched:
                    raise PartialDelegationError(tuple(launched)) from error
                raise
            launched.append(DescendantDispatch(child, handle))
        return tuple(launched)

    def fan_in_hypotheses(
        self,
        proposal: NeedsDelegation,
        dispatched: tuple[DescendantDispatch, ...],
        *,
        artifact_readiness: Iterable[ArtifactReadiness] = (),
        verify_artifact: Callable[[WorkerRecord, SubagentResult], str] | None = None,
    ) -> HypothesisDecision:
        """Rank sealed sibling evidence after a host verifier checks their output.

        A readiness manifest is useful only with the canonical child's bytes,
        the persisted source prompt, and a separately recomputed host verifier
        receipt. The callable must be supplied by a trusted host verifier; its
        output alone is never treated as a model-produced success claim.
        """
        registry = self._worker_registry
        if not isinstance(registry, TerminalChildWorkerRegistry):
            raise IntegrationError("hypothesis fan-in requires a durable child registry")
        if len(proposal.specialists) < 2 or any(not item.hypothesis_digest for item in proposal.specialists):
            raise IntegrationError("hypothesis fan-in requires distinct speculative siblings")
        expected = {item.child_id: item for item in proposal.specialists}
        if len(dispatched) != len(expected) or {item.request.lineage.child_id for item in dispatched} != set(expected):
            raise IntegrationError("hypothesis fan-in requires the complete dispatched sibling set")
        parent = registry.get(proposal.parent_child_id)
        parent_result = registry.terminal_result(proposal.parent_child_id)
        if (
            parent is None or parent_result is None
            or parent_result.status is not SubagentStatus.SUCCEEDED
            or parent.terminal_verification.get("status") != "succeeded"
            or parent.terminal_verification.get("output_digest") != proposal.source_output_digest
            or ResultEvidence.digest(parent_result.output) != proposal.source_output_digest
        ):
            raise IntegrationError("hypothesis proposal differs from its integrated parent")
        root_id, parent_depth = self._recorded_parent_lineage(registry, proposal.parent_child_id)
        sequences = {item.child_id: sequence for sequence, item in enumerate(proposal.specialists, 1)}
        records: dict[str, WorkerRecord] = {}
        results: dict[str, SubagentResult] = {}
        for launched in dispatched:
            child_id = launched.request.lineage.child_id
            item = expected[child_id]
            sequence = sequences[child_id]
            record, result = registry.get(child_id), registry.terminal_result(child_id)
            if record is None or result is None:
                raise IntegrationError("hypothesis worker has no durable terminal result")
            persisted = dict(record.launch.metadata)
            proof = record.terminal_verification
            canonical = result.output if result.status is SubagentStatus.SUCCEEDED else result.error.message
            digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
            if (
                launched.handle.child_id != child_id
                or launched.request.delegation_id != f"{proposal.proposal_id}:delegation:{sequence}"
                or launched.request.lineage.lineage_id != f"{proposal.proposal_id}:lineage:{sequence}"
                or launched.request.lineage.sequence != sequence
                or launched.request.lineage.root_id != root_id
                or launched.request.lineage.depth != parent_depth + 1
                or launched.request.lineage.parent_id != proposal.parent_child_id
                or record.launch.parent_id != proposal.parent_child_id
                or result.parent_id != proposal.parent_child_id
                or launched.request.prompt != item.prompt
                or launched.request.preset.name != item.preset
                or launched.request.workspace != item.workspace
                or launched.request.resource_budget is None
                or any(
                    getattr(launched.request.resource_budget, field) != getattr(item.budget, field)
                    for field in ("max_steps", "max_output_tokens", "max_wall_seconds")
                )
                or any(
                    getattr(item.budget, field) is not None
                    and getattr(launched.request.resource_budget, field) != getattr(item.budget, field)
                    for field in ("max_children", "max_depth", "max_concurrency")
                )
                or launched.request.execution_contract != item.contract
                or launched.request.hypothesis_digest != item.hypothesis_digest
                or launched.request.speculative_lane_id != item.speculative_lane_id
                or persisted.get("request_digest") != delegation_digest(launched.request)
                or persisted.get("hypothesis_digest") != item.hypothesis_digest
                or persisted.get("speculative_lane_id") != item.speculative_lane_id
                or record.launch.execution_contract != item.contract
                or proof.get("status") != ("succeeded" if result.status is SubagentStatus.SUCCEEDED else "failed")
                or proof.get("terminal_status") != result.status.value
                or proof.get("output_digest") != digest
            ):
                raise IntegrationError("hypothesis lacks matching persisted lineage or integrated evidence")
            records[child_id], results[child_id] = record, result
        receipts = tuple(artifact_readiness)
        verified: dict[str, ArtifactReadiness] = {}
        if receipts:
            if verify_artifact is None:
                raise IntegrationError("artifact fan-in requires an independent host verifier")
            succeeded = {
                child for child, result in results.items()
                if result.status is SubagentStatus.SUCCEEDED
            }
            if not succeeded:
                raise IntegrationError("artifact fan-in cannot certify unsuccessful children")
            expected_sources = {
                child: hashlib.sha256(records[child].launch.prompt.encode("utf-8")).hexdigest()
                for child in succeeded
            }
            contents = {child: results[child].output.encode("utf-8") for child in succeeded}
            barrier = ArtifactReadinessBarrier()
            try:
                # Validate completeness, source, bytes and receipt shape before
                # invoking a potentially expensive trusted verifier. This
                # first check does not authorize a supported conclusion.
                barrier.join(
                    receipts,
                    run_id=proposal.proposal_id,
                    expected_producers=tuple(sorted(succeeded)),
                    content_by_producer=contents,
                    expected_source_revisions=expected_sources,
                    expected_verifier_receipts={item.producer_id: item.verifier_receipt
                                                for item in receipts},
                    require_verifier_receipt=True,
                )
                expected_verifiers = {
                    child: verify_artifact(records[child], results[child]) for child in succeeded
                }
                joined = barrier.join(
                    receipts,
                    run_id=proposal.proposal_id,
                    expected_producers=tuple(sorted(succeeded)),
                    content_by_producer=contents,
                    expected_source_revisions=expected_sources,
                    expected_verifier_receipts=expected_verifiers,
                    require_verifier_receipt=True,
                )
            except (TypeError, ValueError) as error:
                raise IntegrationError("artifact fan-in rejected incomplete or stale verification") from error
            verified = {receipt.producer_id: receipt for receipt in joined}
        structured: list[StructuredChildResult] = []
        for child_id in sorted(expected):
            result = results[child_id]
            terminal = records[child_id].terminal_verification
            evidence_digest = terminal["output_digest"]
            receipt = verified.get(child_id)
            annotations = _bounded_child_annotations(result.output)
            failure_code = result.error.code if result.error else ""
            if len(failure_code) > 128:
                failure_code = "sha256:" + hashlib.sha256(failure_code.encode("utf-8")).hexdigest()
            structured.append(StructuredChildResult(
                child_id=child_id,
                status=result.status,
                conclusion=("supported" if receipt else "rejected"
                            if result.status is not SubagentStatus.SUCCEEDED else "unresolved"),
                output_digest=evidence_digest,
                usage=result.usage,
                evidence_refs=(evidence_digest,),
                failure_code=failure_code,
                verifier_receipts=(receipt.verifier_receipt,) if receipt else (),
                artifact_refs=(receipt.content_digest,) if receipt else (),
                assumptions=annotations.get("assumptions", ()),
                unresolved_questions=annotations.get("unresolved_questions", ()),
                suggested_actions=annotations.get("suggested_actions", ()),
            ))
        ranked = tuple(sorted(structured, key=lambda child: (
            {"supported": 0, "unresolved": 1, "rejected": 2}[child.conclusion],
            child.usage.steps, child.child_id,
        )))
        winner = ranked[0].child_id if ranked[0].conclusion == "supported" else None
        failed = tuple(item for item in ranked if item.conclusion == "rejected")
        if winner:
            reason = "host-verified artifact evidence"
        elif failed:
            detail = ", ".join(f"{item.child_id}={item.failure_code}" for item in failed)
            reason = ("all hypotheses failed: " if len(failed) == len(ranked)
                      else "no independently verified winning hypothesis; failed: ") + detail
        else:
            reason = "no independently verified winning hypothesis"
        return HypothesisDecision(
            winner,
            tuple(item.child_id for item in ranked),
            tuple(ranked),
            reason,
        )

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


def _bounded_child_annotations(output: str) -> dict[str, tuple[str, ...]]:
    """Retain only digests of short notes, never child text or secrets."""
    if len(output) > 4000:
        return {}
    try:
        parsed = json.loads(output)
    except (TypeError, ValueError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    selected: dict[str, tuple[str, ...]] = {}
    for field in ("assumptions", "unresolved_questions", "suggested_actions"):
        values = parsed.get(field, ())
        if (
            isinstance(values, list) and len(values) <= 16
            and all(isinstance(value, str) and value.strip() and len(value) <= 512 for value in values)
        ):
            selected[field] = tuple(hashlib.sha256(value.strip().encode()).hexdigest() for value in values)
    return selected


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True
