"""AGENT-010 role workflow integration contracts."""
from __future__ import annotations

from pathlib import Path

import pytest

from sonder_runtime.application.agents.delegation_service import DelegationService
from sonder_runtime.application.agents.lineage_delegation import (
    IntegrationError,
    WorkspaceAssignment,
)
from sonder_runtime.application.agents.workflow_integration import (
    AgentWorkflowService,
    AgentWorkflowStatus,
    WorkflowDispatch,
)
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports.subagents import (
    SubagentError,
    SubagentResult,
    SubagentStatus,
    SubagentUsage,
)
from sonder_runtime.domain.agents.roles import AgentRole


class _Handle:
    def __init__(self, child_id: str, parent_id: str) -> None:
        self.child_id, self.parent_id = child_id, parent_id

    def cancel(self, *, reason: str = "cancellation requested") -> bool:
        return True

    def result(self, timeout: float | None = None):
        raise AssertionError("test handle is advanced with an explicit result")

    def snapshot(self):
        raise AssertionError("test handle has no snapshot projection")


class _Provider:
    def __init__(self) -> None:
        self.requests = []

    def spawn(self, request, context):
        self.requests.append((request, context))
        return _Handle(request.child_id, request.parent_id)


def _setup(tmp_path: Path):
    provider = _Provider()
    service = AgentWorkflowService(DelegationService(provider))
    root = tmp_path / "repo"
    workspace = WorkspaceAssignment((str(root),), (str(root / "out"),))
    context = local_owner_context(correlation_id="agent-010", workspace_roots=(root,))
    return provider, service, workspace, context


def _success(dispatch, text: str) -> SubagentResult:
    return SubagentResult(
        dispatch.handle.child_id, dispatch.handle.parent_id, SubagentStatus.SUCCEEDED,
        output=text, usage=SubagentUsage(steps=2),
    )


def test_full_role_workflow_routes_presets_and_builds_durable_lineage(tmp_path):
    provider, service, workspace, context = _setup(tmp_path)
    dispatch = service.start(
        workflow_id="wf-010", root_id="root-session", parent_id="root-session",
        prompt="understand and implement the change", workspace=workspace, context=context,
    )

    expected = (
        AgentRole.EXPLORER, AgentRole.ARCHITECT, AgentRole.EDITOR,
        AgentRole.VERIFIER, AgentRole.REVIEWER, AgentRole.INTEGRATOR,
    )
    seen = []
    while dispatch is not None:
        seen.append(dispatch.request.lineage.role)
        assert dispatch.request.lineage.workspace == workspace
        advance = service.advance(
            dispatch, _success(dispatch, f"{dispatch.request.lineage.role.value} complete"),
            verification=(f"{dispatch.request.lineage.role.value} checked",), context=context,
        )
        dispatch = advance.next_dispatch

    assert tuple(seen) == expected
    assert advance.state.status is AgentWorkflowStatus.SUCCEEDED
    assert tuple(item.role for item in advance.state.results) == expected
    assert all(item.evidence.verification for item in advance.state.results)
    requests = [request for request, _ in provider.requests]
    preset_by_role = {
        AgentRole.EXPLORER: "general", AgentRole.ARCHITECT: "plan",
        AgentRole.EDITOR: "code", AgentRole.VERIFIER: "build-test",
        AgentRole.REVIEWER: "reviewer", AgentRole.INTEGRATOR: "integrator",
    }
    assert tuple(dict(request.metadata)["preset"] for request in requests) == tuple(
        preset_by_role[role] for role in expected
    )
    assert requests[0].parent_id == "root-session"
    assert all(request.parent_id == "root-session" for request in requests)
    assert all(request.child_id != request.parent_id for request in requests)
    assert tuple(request.child_id for request in requests) == tuple(
        f"wf-010:{index}:{role.value}" for index, role in enumerate(expected, 1)
    )


def test_sequential_roles_share_durable_parent_and_can_use_independent_presets(tmp_path):
    from sonder_runtime.adapters.persistence.durable_continuation import (
        SQLiteDurableContinuationRepository,
    )
    from sonder_runtime.adapters.subagents import RunnerBoundSubagentProvider
    from sonder_runtime.application.ports.subagents import SubagentBudget
    from sonder_runtime.application.subagents.durable_continuation import (
        DurableContinuationService,
    )
    from sonder_runtime.application.worker_registry.continuation import (
        ContinuationWorkerRegistry,
    )

    repository = SQLiteDurableContinuationRepository(tmp_path / "roles.sqlite")
    child_service = DurableContinuationService(repository)
    child_service.register_root(
        "root", SubagentBudget(max_steps=30, max_output_tokens=8000, max_wall_seconds=900)
    )
    provider = RunnerBoundSubagentProvider(child_service, lambda state, save, control: "observed")
    workflow = AgentWorkflowService(
        DelegationService(provider, worker_registry=ContinuationWorkerRegistry(repository)),
        roles=(AgentRole.EXPLORER, AgentRole.ARCHITECT, AgentRole.EDITOR),
    )
    root = tmp_path / "repo"
    context = local_owner_context(correlation_id="sequential", workspace_roots=(root,))
    dispatch = workflow.start(
        workflow_id="sequential", root_id="root", parent_id="root", prompt="inspect",
        workspace=WorkspaceAssignment((str(root),), ()), context=context,
    )
    seen = []
    while dispatch is not None:
        seen.append(dispatch.request.lineage)
        advance = workflow.advance(dispatch, dispatch.handle.result(timeout=2), context=context)
        dispatch = advance.next_dispatch
    assert advance.state.status is AgentWorkflowStatus.SUCCEEDED
    assert [row.depth for row in seen] == [1, 1, 1]
    assert [row.parent_id for row in seen] == ["root", "root", "root"]
    assert [repository.get(row.child_id).lineage.chain for row in seen] == [
        ("root",), ("root",), ("root",),
    ]
    assert [repository.get(row.child_id).request.budget.max_steps for row in seen] == [8, 12, 20]


def test_sequential_roles_preserve_nested_workflow_parent(tmp_path):
    provider = _Provider()
    workflow = AgentWorkflowService(
        DelegationService(provider), roles=(AgentRole.EXPLORER, AgentRole.ARCHITECT)
    )
    root = tmp_path / "repo"
    context = local_owner_context(correlation_id="nested", workspace_roots=(root,))
    dispatch = workflow.start(
        workflow_id="nested", root_id="original-root", parent_id="registered-parent",
        prompt="inspect", workspace=WorkspaceAssignment((str(root),), ()), context=context,
    )
    following = workflow.advance(dispatch, _success(dispatch, "evidence"), context=context).next_dispatch
    assert following is not None
    assert [request.parent_id for request, _ in provider.requests] == [
        "registered-parent", "registered-parent",
    ]
    assert following.request.lineage.root_id == "original-root"
    assert following.request.lineage.depth == dispatch.request.lineage.depth == 1


def test_sequential_roles_still_obey_parent_budget(tmp_path):
    from sonder_runtime.adapters.persistence.durable_continuation import (
        SQLiteDurableContinuationRepository,
    )
    from sonder_runtime.adapters.subagents import RunnerBoundSubagentProvider
    from sonder_runtime.application.ports.subagents import (
        InvalidSubagentRequest,
        SubagentBudget,
    )
    from sonder_runtime.application.subagents.durable_continuation import (
        DurableContinuationService,
    )
    from sonder_runtime.application.worker_registry.continuation import (
        ContinuationWorkerRegistry,
    )

    repository = SQLiteDurableContinuationRepository(tmp_path / "bounded.sqlite")
    service = DurableContinuationService(repository)
    service.register_root(
        "root", SubagentBudget(max_steps=8, max_output_tokens=2000, max_wall_seconds=120)
    )
    workflow = AgentWorkflowService(
        DelegationService(
            RunnerBoundSubagentProvider(service, lambda state, save, control: "done"),
            worker_registry=ContinuationWorkerRegistry(repository),
        ),
        roles=(AgentRole.EXPLORER, AgentRole.ARCHITECT),
    )
    root = tmp_path / "repo"
    context = local_owner_context(correlation_id="bounded", workspace_roots=(root,))
    dispatch = workflow.start(
        workflow_id="bounded", root_id="root", parent_id="root", prompt="inspect",
        workspace=WorkspaceAssignment((str(root),), ()), context=context,
    )
    with pytest.raises(InvalidSubagentRequest, match="widens parent max_steps"):
        workflow.advance(dispatch, dispatch.handle.result(timeout=2), context=context)


def test_failed_role_result_is_terminal_and_does_not_dispatch_later_roles(tmp_path):
    provider, service, workspace, context = _setup(tmp_path)
    dispatch = service.start(
        workflow_id="wf-fail", root_id="root", parent_id="root", prompt="inspect",
        workspace=workspace, context=context,
    )
    result = SubagentResult(
        dispatch.handle.child_id, dispatch.handle.parent_id, SubagentStatus.FAILED,
        error=SubagentError("verifier_failed", "verification failed", retryable=True),
    )
    advance = service.advance(dispatch, result, context=context)
    assert advance.state.status is AgentWorkflowStatus.FAILED
    assert advance.next_dispatch is None
    assert len(provider.requests) == 1
    assert advance.state.results[0].evidence.failure_reason == "verification failed"


def test_workflow_rejects_stale_dispatch_result(tmp_path):
    _, service, workspace, context = _setup(tmp_path)
    dispatch = service.start(
        workflow_id="wf-stale", root_id="root", parent_id="root", prompt="inspect",
        workspace=workspace, context=context,
    )
    forged_state = type(dispatch.state)(
        dispatch.state.workflow_id, dispatch.state.root_id, dispatch.state.operation_id,
        dispatch.state.roles, AgentWorkflowStatus.RUNNING, dispatch.state.active_role,
        "different-delegation", dispatch.state.revision, dispatch.state.results,
    )
    forged = WorkflowDispatch(forged_state, dispatch.request, dispatch.handle)
    with pytest.raises(IntegrationError, match="active workflow delegation"):
        service.advance(forged, _success(dispatch, "unexpected"), context=context)


def test_workspace_context_remains_fail_closed_for_workflow_start(tmp_path):
    provider = _Provider()
    service = AgentWorkflowService(DelegationService(provider))
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    context = local_owner_context(correlation_id="agent-010-deny", workspace_roots=(allowed,))
    assignment = WorkspaceAssignment((str(outside),), ())
    with pytest.raises(IntegrationError, match="outside the parent context"):
        service.start(
            workflow_id="wf-deny", root_id="root", parent_id="root", prompt="inspect",
            workspace=assignment, context=context,
        )
    assert provider.requests == []
