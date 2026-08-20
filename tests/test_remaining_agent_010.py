"""AGENT-010 role workflow integration contracts."""
from __future__ import annotations

from pathlib import Path

import pytest

from sonder_runtime.application.agents.delegation_service import DelegationService
from sonder_runtime.application.agents.lineage_delegation import IntegrationError, WorkspaceAssignment
from sonder_runtime.application.agents.workflow_integration import (
    AgentWorkflowService, AgentWorkflowStatus, WorkflowDispatch,
)
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports.subagents import (
    SubagentError, SubagentResult, SubagentStatus, SubagentUsage,
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
    assert all(left.child_id == right.parent_id for left, right in zip(requests, requests[1:]))
    assert tuple(request.child_id for request in requests) == tuple(
        f"wf-010:{index}:{role.value}" for index, role in enumerate(expected, 1)
    )


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
