"""Provider-neutral multi-role agent workflow orchestration (AGENT-010).

This module owns workflow state transitions only.  Child execution remains
owned by ``SubagentProvider`` implementations and durable parent/child
lineage remains owned by the provider's persistence adapter.  Every dispatch
still passes through ``DelegationService``, so preset budgets, workspace
containment, and result evidence cannot be bypassed by the workflow.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Iterable

logger = logging.getLogger(__name__)

from sonder_runtime.application.agents.delegation_service import (
    DelegatedResult,
    DelegationService,
)
from sonder_runtime.application.agents.lineage_delegation import (
    DelegationRequest,
    DelegationStatus,
    IntegrationError,
    LineageRecord,
    ResultEvidence,
    WorkspaceAssignment,
    complete_builtin_presets,
    default_role_workflow,
)
from sonder_runtime.application.context import OperationContext
from sonder_runtime.application.ports.subagents import (
    SubagentHandle,
    SubagentResult,
)
from sonder_runtime.domain.agents.roles import AgentRole


MAX_WORKFLOW_ID_CHARS = 128
MAX_WORKFLOW_PROMPT_CHARS = 16_000


class AgentWorkflowStatus(str, Enum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class WorkflowStepResult:
    """The evidence-backed result of one role in the workflow."""

    role: AgentRole
    delegation_id: str
    child_id: str
    evidence: ResultEvidence


@dataclass(frozen=True, slots=True)
class AgentWorkflowState:
    """Immutable state projection safe to checkpoint alongside durable lineage."""

    workflow_id: str
    root_id: str
    operation_id: str
    roles: tuple[AgentRole, ...]
    status: AgentWorkflowStatus
    active_role: AgentRole | None
    active_delegation_id: str | None
    revision: int = 0
    results: tuple[WorkflowStepResult, ...] = ()

    def __post_init__(self) -> None:
        for name in ("workflow_id", "root_id", "operation_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise IntegrationError(f"{name} must be non-empty")
            if len(value) > MAX_WORKFLOW_ID_CHARS:
                raise IntegrationError(f"{name} exceeds its bound")
        if not self.roles or len(set(self.roles)) != len(self.roles):
            raise IntegrationError("workflow roles must be unique and non-empty")
        if self.revision < 0 or len(self.results) > len(self.roles):
            raise IntegrationError("workflow revision/results are out of bounds")
        if self.status is AgentWorkflowStatus.RUNNING:
            if self.active_role is None or not self.active_delegation_id:
                raise IntegrationError("running workflow requires an active delegation")
        elif self.active_role is not None or self.active_delegation_id is not None:
            raise IntegrationError("terminal workflow cannot have an active delegation")

    @property
    def next_role(self) -> AgentRole | None:
        if len(self.results) >= len(self.roles):
            return None
        return self.roles[len(self.results)]


@dataclass(frozen=True, slots=True)
class WorkflowDispatch:
    state: AgentWorkflowState
    request: DelegationRequest
    handle: SubagentHandle


@dataclass(frozen=True, slots=True)
class WorkflowAdvance:
    state: AgentWorkflowState
    completed: DelegatedResult
    next_dispatch: WorkflowDispatch | None = None


class AgentWorkflowService:
    """Advance the independently routed, evidence-backed role workflow."""

    def __init__(
        self,
        delegation: DelegationService,
        *,
        roles: Iterable[AgentRole] | None = None,
    ) -> None:
        self._delegation = delegation
        catalog = {preset.role: preset for preset in complete_builtin_presets()}
        ordered = tuple(roles) if roles is not None else default_role_workflow().topological_order()
        if not ordered or len(set(ordered)) != len(ordered):
            raise IntegrationError("workflow roles must be unique and non-empty")
        if any(role not in catalog for role in ordered):
            raise IntegrationError("workflow role has no built-in preset")
        self._roles = ordered
        self._presets = catalog

    @property
    def roles(self) -> tuple[AgentRole, ...]:
        return self._roles

    def start(
        self,
        *,
        workflow_id: str,
        root_id: str,
        parent_id: str,
        prompt: str,
        workspace: WorkspaceAssignment,
        context: OperationContext,
    ) -> WorkflowDispatch:
        """Create and dispatch the explorer (or configured first) role."""
        logger.debug(f"AgentWorkflowService.start: workflow_id={workflow_id!r}, root_id={root_id!r}, first_role={self._roles[0].value!r}, num_roles={len(self._roles)}")
        logger.info(f"agent workflow starting: workflow_id={workflow_id!r}, roles={[r.value for r in self._roles]}, first_role={self._roles[0].value!r}")
        self._required(workflow_id, "workflow_id")
        self._required(root_id, "root_id")
        self._required(parent_id, "parent_id")
        self._prompt(prompt)
        state = AgentWorkflowState(
            workflow_id, root_id, context.correlation_id, self._roles,
            AgentWorkflowStatus.RUNNING, self._roles[0],
            f"{workflow_id}:delegation:1",
        )
        return self._dispatch(state, parent_id=parent_id, prompt=prompt, workspace=workspace, context=context)

    def advance(
        self,
        dispatch: WorkflowDispatch,
        result: SubagentResult,
        *,
        verification: Iterable[str] = (),
        artifacts: Iterable[str] = (),
        context: OperationContext,
    ) -> WorkflowAdvance:
        """Integrate one result and dispatch the next role when safe.

        A failed, cancelled, or timed-out child terminates the workflow and
        cannot cause a later editor or integrator to run.
        """
        logger.debug(f"AgentWorkflowService.advance: workflow_id={dispatch.state.workflow_id!r}, active_role={dispatch.state.active_role!r}, result_status={result.status.value!r}")
        state = dispatch.state
        if state.active_delegation_id != dispatch.request.delegation_id:
            raise IntegrationError("dispatch is not the active workflow delegation")
        completed = self._delegation.integrate(
            dispatch.request, result, verification=verification, artifacts=artifacts,
        )
        active_role = state.active_role
        if active_role is None:
            raise IntegrationError("workflow has no active role")
        step = WorkflowStepResult(
            active_role, dispatch.request.delegation_id, result.child_id, completed.evidence,
        )
        success = completed.evidence.status is DelegationStatus.SUCCEEDED
        results = state.results + (step,)
        if not success:
            logger.error(f"workflow step failed, terminating workflow: workflow_id={state.workflow_id!r}, role={active_role.value!r}, delegation_id={dispatch.request.delegation_id!r}, completed_steps={len(results)}/{len(state.roles)}")
            logger.warning(f"workflow step failed, terminating workflow: workflow_id={state.workflow_id!r}, role={active_role.value!r}, completed_steps={len(results)}/{len(state.roles)}")
            logger.info(f"agent workflow step failed: workflow_id={state.workflow_id!r}, role={active_role.value!r}, terminating workflow")
            logger.debug(f"AgentWorkflowService.advance: workflow {state.workflow_id!r} step failed, terminating")
            terminal = AgentWorkflowState(
                state.workflow_id, state.root_id, state.operation_id, state.roles,
                AgentWorkflowStatus.FAILED, None, None, state.revision + 1, results,
            )
            return WorkflowAdvance(terminal, completed)

        next_role = state.roles[len(results)] if len(results) < len(state.roles) else None
        if next_role is None:
            logger.info(f"agent workflow succeeded: workflow_id={state.workflow_id!r}, completed_steps={len(results)}/{len(state.roles)}")
            logger.debug(f"AgentWorkflowService.advance: workflow {state.workflow_id!r} all roles complete, succeeding")
            terminal = AgentWorkflowState(
                state.workflow_id, state.root_id, state.operation_id, state.roles,
                AgentWorkflowStatus.SUCCEEDED, None, None, state.revision + 1, results,
            )
            return WorkflowAdvance(terminal, completed)

        next_state = AgentWorkflowState(
            state.workflow_id, state.root_id, state.operation_id, state.roles,
            AgentWorkflowStatus.RUNNING, next_role,
            f"{state.workflow_id}:delegation:{len(results) + 1}",
            state.revision + 1, results,
        )
        next_prompt = self._next_prompt(next_role, completed.result.output)
        if len(next_prompt) > MAX_WORKFLOW_PROMPT_CHARS * 0.9:
            logger.warning(f"workflow prompt approaching size limit: workflow_id={state.workflow_id!r}, prompt_len={len(next_prompt)}/{MAX_WORKFLOW_PROMPT_CHARS}")
        logger.info(f"agent workflow advancing: workflow_id={state.workflow_id!r}, next_role={next_role.value!r}, step={len(results)+1}/{len(state.roles)}")
        logger.debug(f"AgentWorkflowService.advance: workflow {state.workflow_id!r} advancing to next_role={next_role.value!r}, step={len(results)+1}/{len(state.roles)}")
        next_dispatch = self._dispatch(
            next_state, parent_id=result.child_id, prompt=next_prompt,
            workspace=dispatch.request.workspace, context=context,
        )
        return WorkflowAdvance(next_dispatch.state, completed, next_dispatch)

    def _dispatch(
        self,
        state: AgentWorkflowState,
        *,
        parent_id: str,
        prompt: str,
        workspace: WorkspaceAssignment,
        context: OperationContext,
    ) -> WorkflowDispatch:
        logger.debug(f"AgentWorkflowService._dispatch: workflow_id={state.workflow_id!r}, active_role={state.active_role!r}")
        role = state.active_role
        if role is None:
            raise IntegrationError("cannot dispatch a terminal workflow")
        preset = self._presets[role]
        child_id = f"{state.workflow_id}:{len(state.results) + 1}:{role.value}"
        delegation_id = f"{state.workflow_id}:delegation:{len(state.results) + 1}"
        lineage = LineageRecord(
            f"{state.workflow_id}:lineage:{len(state.results) + 1}",
            state.root_id, parent_id, child_id, len(state.results) + 1,
            preset.name, role, workspace, sequence=len(state.results),
        )
        request = DelegationRequest(
            delegation_id, lineage, self._prompt(prompt), preset, workspace,
            ("agent-workflow", role.value),
        )
        handle = self._delegation.dispatch(request, context)
        active = AgentWorkflowState(
            state.workflow_id, state.root_id, state.operation_id, state.roles,
            state.status, role, delegation_id, state.revision, state.results,
        )
        return WorkflowDispatch(active, request, handle)

    @staticmethod
    def _required(value: str, field: str) -> str:
        if not isinstance(value, str) or not value.strip() or len(value.strip()) > MAX_WORKFLOW_ID_CHARS:
            raise IntegrationError(f"{field} is empty or exceeds its bound")
        return value.strip()

    @staticmethod
    def _prompt(value: str) -> str:
        if not isinstance(value, str) or not value.strip() or len(value) > MAX_WORKFLOW_PROMPT_CHARS:
            raise IntegrationError("workflow prompt is empty or exceeds its bound")
        return value

    @staticmethod
    def _next_prompt(role: AgentRole, previous_output: str) -> str:
        prefix = f"Continue the agent workflow as the {role.value} role. Previous role result:\n"
        return AgentWorkflowService._prompt(prefix + previous_output)


__all__ = [
    "AgentWorkflowService", "AgentWorkflowState", "AgentWorkflowStatus",
    "WorkflowAdvance", "WorkflowDispatch", "WorkflowStepResult",
]
