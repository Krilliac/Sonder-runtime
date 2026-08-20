from pathlib import Path

import pytest

from sonder_runtime.application.agents.delegation_service import DelegationService
from sonder_runtime.application.agents.lineage_delegation import (
    DelegationRequest, IntegrationError, LineageRecord, WorkspaceAssignment,
)
from sonder_runtime.application.agents.presets import builtin_presets, resolve_preset
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports.subagents import (
    SubagentError, SubagentResult, SubagentStatus, SubagentUsage,
)
from sonder_runtime.domain.agents.roles import AgentRole


class _Handle:
    child_id = "child-1"
    parent_id = "parent-1"


class _Provider:
    def __init__(self):
        self.requests = []

    def spawn(self, request, context):
        self.requests.append((request, context))
        return _Handle()


class _Events:
    def __init__(self):
        self.rows = []

    def emit(self, event_code, **kwargs):
        self.rows.append((event_code, kwargs))


def _request(root: Path) -> DelegationRequest:
    workspace = WorkspaceAssignment((str(root),), (str(root / "write"),))
    preset = resolve_preset("researcher")
    lineage = LineageRecord("line-1", "root-1", "parent-1", "child-1", 1, preset.name, preset.role, workspace)
    return DelegationRequest("delegation-1", lineage, "research the change", preset, workspace)


def test_typed_catalog_contains_researcher_and_stable_capabilities():
    catalog = builtin_presets()
    assert tuple(item.name for item in catalog) == (
        "general", "code", "plan", "reviewer", "researcher", "build-test",
    )
    researcher = resolve_preset("RESEARCHER")
    assert researcher.role is AgentRole.EXPLORER
    assert "research" in researcher.capabilities


def test_workspace_guard_enforces_read_write_containment(tmp_path):
    root = tmp_path / "repo"
    write = root / "write"
    outside = tmp_path / "outside"
    assignment = WorkspaceAssignment((str(root),), (str(write),))
    assert assignment.permits(root / "src" / "main.py")
    assert assignment.permits(write / "out.txt", write=True)
    assert not assignment.permits(root / "src" / "main.py", write=True)
    assert not assignment.permits(outside / "escape.txt")
    with pytest.raises(IntegrationError, match="outside explicit assignment"):
        assignment.guard().require(outside / "escape.txt")


def test_delegation_service_uses_subagent_port_and_integrates_result(tmp_path):
    provider = _Provider()
    events = _Events()
    service = DelegationService(provider, events)
    root = tmp_path / "repo"
    request = _request(root)
    context = local_owner_context(correlation_id="corr-1", workspace_roots=(root,))
    handle = service.dispatch(request, context)
    child_request, child_context = provider.requests[0]
    assert handle.child_id == request.lineage.child_id
    assert child_request.metadata[0] == ("delegation_id", request.delegation_id)
    assert child_request.metadata[2] == ("preset", "researcher")
    assert child_context is context
    assert events.rows[0][0] == "agent.delegation.accepted"

    result = SubagentResult(
        "child-1", "parent-1", SubagentStatus.SUCCEEDED,
        output="research complete", usage=SubagentUsage(steps=3),
    )
    integrated = service.integrate(request, result, verification=("tests passed",), artifacts=("report.md",))
    assert integrated.evidence.verification == ("tests passed",)
    assert integrated.evidence.artifacts == ("report.md",)
    assert integrated.evidence.usage_steps == 3


def test_delegation_service_rejects_workspace_outside_parent_context(tmp_path):
    provider = _Provider()
    service = DelegationService(provider)
    request = _request(tmp_path / "other")
    context = local_owner_context(correlation_id="corr-2", workspace_roots=(tmp_path / "allowed",))
    with pytest.raises(IntegrationError, match="outside the parent context"):
        service.dispatch(request, context)


def test_result_integration_rejects_mismatched_lineage(tmp_path):
    request = _request(tmp_path / "repo")
    result = SubagentResult(
        "other-child", "parent-1", SubagentStatus.FAILED,
        error=SubagentError("failed", "nope"),
    )
    with pytest.raises(IntegrationError, match="does not match"):
        DelegationService(_Provider()).integrate(request, result)
