from sonder_runtime.application.agents.lineage_delegation import (
    DelegationRequest,
    DelegationStatus,
    IntegrationError,
    LineageRecord,
    ResultEvidence,
    RoleWorkflowGraph,
    WorkspaceAssignment,
    complete_builtin_presets,
    default_role_workflow,
    delegation_digest,
)
from sonder_runtime.domain.agents.roles import AgentRole


def _request() -> DelegationRequest:
    workspace = WorkspaceAssignment(("repo", "docs"), ("repo",))
    preset = next(item for item in complete_builtin_presets() if item.role is AgentRole.EDITOR)
    lineage = LineageRecord("lin-1", "root-1", "parent-1", "child-1", 1, preset.name, preset.role, workspace)
    return DelegationRequest("del-1", lineage, "implement the change", preset, workspace, ("plan", "test"))


def test_complete_catalog_has_all_domain_roles_and_stable_order():
    presets = complete_builtin_presets()
    assert tuple(item.role for item in presets) == tuple(AgentRole)
    assert {item.name for item in presets} == {"general", "plan", "code", "reviewer", "build-test", "integrator"}


def test_lineage_workspace_and_delegation_are_explicit_and_digestable():
    request = _request()
    assert request.workspace.permits_write("repo")
    assert not request.workspace.permits_write("docs")
    assert delegation_digest(request) == delegation_digest(request)
    assert len(delegation_digest(request)) == 64


def test_workspace_rejects_implicit_write_root():
    try:
        WorkspaceAssignment(("repo",), ("unlisted",))
    except IntegrationError as exc:
        assert "read root" in str(exc)
    else:
        raise AssertionError("expected explicit read-root requirement")


def test_result_evidence_is_structured_and_bounded():
    digest = ResultEvidence.digest("verified output")
    evidence = ResultEvidence("del-1", DelegationStatus.SUCCEEDED, digest, ("tests passed",), ("patch.diff",), usage_steps=3)
    assert evidence.output_digest == digest
    try:
        ResultEvidence("del-1", DelegationStatus.FAILED, digest)
    except IntegrationError as exc:
        assert "failure reason" in str(exc)
    else:
        raise AssertionError("expected failure reason requirement")


def test_default_role_graph_is_ordered_and_cycles_are_rejected():
    graph = default_role_workflow()
    order = graph.topological_order()
    assert order.index(AgentRole.EXPLORER) < order.index(AgentRole.INTEGRATOR)
    assert graph.successors(AgentRole.REVIEWER) == (AgentRole.INTEGRATOR,)
    try:
        RoleWorkflowGraph(((AgentRole.EDITOR, AgentRole.REVIEWER), (AgentRole.REVIEWER, AgentRole.EDITOR)))
    except IntegrationError as exc:
        assert "acyclic" in str(exc)
    else:
        raise AssertionError("expected cycle rejection")
