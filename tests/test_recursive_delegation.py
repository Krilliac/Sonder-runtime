"""Host-reviewed descendants and sealed, evidence-based hypothesis fan-in."""

import hashlib
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from threading import Event

import pytest

from sonder_runtime.adapters.persistence.durable_continuation import (
    SQLiteDurableContinuationRepository,
)
from sonder_runtime.adapters.subagents import LocalSubagentProvider
from sonder_runtime.application.agents.delegation_service import DelegationService
from sonder_runtime.application.agents.lineage_delegation import (
    DelegationRequest,
    IntegrationError,
    LineageRecord,
    ResultEvidence,
    WorkspaceAssignment,
)
from sonder_runtime.application.agents.presets import resolve_preset
from sonder_runtime.application.agents.recursive_delegation import (
    NeedsDelegation,
    PartialDelegationError,
    SpecialistRequest,
)
from sonder_runtime.application.artifacts.readiness import ArtifactReadiness
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports.subagents import (
    InvalidSubagentRequest,
    SubagentBudget,
    SubagentStatus,
)
from sonder_runtime.application.ports.worker_registry import (
    WorkerExecutionContract,
    WorkerRegistryError,
)
from sonder_runtime.application.subagents.durable_continuation import (
    DurableContinuationService,
)
from sonder_runtime.application.worker_registry.continuation import (
    ContinuationWorkerRegistry,
)


def _digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _setup(tmp_path, *, host_root=False, hold_child=None):
    repo = SQLiteDurableContinuationRepository(tmp_path / "children.sqlite")
    service = DurableContinuationService(repo)
    root_budget = SubagentBudget(
        max_children=8, max_depth=2, max_concurrency=3,
        max_steps=36, max_output_tokens=8000, max_wall_seconds=600,
    )
    registry = ContinuationWorkerRegistry(repo, owner_nonce=service.owner_nonce,
                                           owner_pid=service.owner_pid, owner_host=service.owner_host)
    context = local_owner_context(correlation_id="run-1", workspace_roots=(tmp_path / "repo",))
    root_id = DelegationService.root_id_for_context(context) if host_root else "root"
    if not host_root:
        service.register_root(root_id, root_budget)

    def factory(request, _context):
        def runner(state, save, control):
            save({"step": 1})
            if request.child_id == "hyp-1" and hold_child is not None:
                assert hold_child.wait(timeout=3)
            return '{"assumptions":["requires checked inputs"],"suggested_actions":["review"]}'
        return runner

    provider = LocalSubagentProvider(service, runner_factory=factory)
    delegation = DelegationService(
        provider, worker_registry=registry,
        **({"host_root_budget": root_budget,
            "register_host_root": lambda root, budget, owner: provider.register_root(
                root, budget, owner_id=owner)} if host_root else {}),
    )
    workspace = WorkspaceAssignment((str(tmp_path / "repo"),), ())
    preset = resolve_preset("researcher")
    lineage = LineageRecord("parent-lineage", root_id, root_id, "parent", 1,
                            preset.name, preset.role, workspace)
    request = DelegationRequest("parent-delegation", lineage, "compare two hypotheses", preset, workspace)
    return repo, service, registry, delegation, request, context, root_budget


def _proposal(parent_result, workspace, *, budget=None):
    budget = budget or SubagentBudget(max_steps=3, max_output_tokens=400, max_wall_seconds=20)
    specialists = tuple(SpecialistRequest(
        child_id=f"hyp-{index}", preset="researcher", prompt=f"test hypothesis {index}",
        workspace=workspace, budget=budget,
        contract=WorkerExecutionContract(task_scope="compare-answer", speculative_lane=True),
        hypothesis_digest=_digest(f"test hypothesis {index}"), speculative_lane_id=f"lane-{index}",
    ) for index in (1, 2))
    return NeedsDelegation("parent", ResultEvidence.digest(parent_result.output), specialists, "run-1:hyp")


def _parent(delegation, request, context):
    result = delegation.dispatch(request, context).result(timeout=3)
    delegation.integrate(request, result)
    return result


def _verified_readiness(proposal, dispatched, registry):
    return tuple(ArtifactReadiness.from_content(
        child.request.lineage.child_id,
        proposal.proposal_id,
        registry.terminal_result(child.handle.child_id).output,
        source_revision=_digest(registry.get(child.handle.child_id).launch.prompt),
        verifier_receipt=_digest("host-check:" + registry.terminal_result(child.handle.child_id).output),
    ) for child in dispatched)


def _host_verifier(_record, result):
    return _digest("host-check:" + result.output)


def test_recursive_hypotheses_restart_then_fan_in_sealed_results(tmp_path):
    repo, _, registry, delegation, request, context, _ = _setup(tmp_path)
    parent = _parent(delegation, request, context)
    proposal = _proposal(parent, request.workspace)
    dispatched = delegation.dispatch_proposal(request, parent, proposal, context=context)
    for child in dispatched:
        result = child.handle.result(timeout=3)
        delegation.integrate(child.request, result)
        assert registry.get(child.handle.child_id).launch.budgets["max_depth"] == 2
        assert registry.get(child.handle.child_id).launch.budgets["max_children"] == 2

    reopened = SQLiteDurableContinuationRepository(tmp_path / "children.sqlite")
    restarted = DurableContinuationService(reopened)
    second_registry = ContinuationWorkerRegistry(reopened)
    second = DelegationService(LocalSubagentProvider(restarted, runner_factory=lambda *_: (
        lambda *_: pytest.fail("a terminal child must not restart"))), worker_registry=second_registry)
    replayed = second.dispatch_proposal(request, parent, proposal, context=context)
    assert {item.handle.child_id for item in replayed} == {"hyp-1", "hyp-2"}
    assert [item.request.child_id for item in repo.list_active()] == ["root"]

    assert second.fan_in_hypotheses(proposal, replayed).winning_child_id is None
    readiness = _verified_readiness(proposal, replayed, second_registry)
    decision = second.fan_in_hypotheses(
        proposal, replayed, artifact_readiness=readiness, verify_artifact=_host_verifier,
    )
    assert decision.winning_child_id == "hyp-1"
    assert decision.ranking == ("hyp-1", "hyp-2")
    assert all(item.conclusion == "supported" for item in decision.results)
    assert all(item.assumptions == (_digest("requires checked inputs"),) for item in decision.results)
    assert all(len(item.evidence_refs) == 1 and item.verifier_receipts for item in decision.results)
    assert all(not item.mutations for item in decision.results)


def test_proposal_rejects_forged_parent_budget_workspace_and_duplicate_hypotheses(tmp_path):
    repo, _, registry, delegation, request, context, _ = _setup(tmp_path)
    parent = _parent(delegation, request, context)
    proposal = _proposal(parent, request.workspace)
    assert repo.get("hyp-1") is None
    with pytest.raises(IntegrationError, match="durable successful parent"):
        delegation.dispatch_proposal(request, replace(parent, output="forged"), proposal, context=context)
    with pytest.raises(IntegrationError, match="durable successful parent"):
        delegation.dispatch_proposal(request, parent, replace(proposal, source_output_digest=_digest("other")), context=context)
    with pytest.raises(IntegrationError, match="widens remaining parent"):
        delegation.dispatch_proposal(request, parent, _proposal(
            parent, request.workspace,
            budget=SubagentBudget(max_steps=8, max_output_tokens=400, max_wall_seconds=20)), context=context)
    with pytest.raises(IntegrationError, match="workspace widens"):
        outside = replace(proposal.specialists[0], workspace=WorkspaceAssignment((str(tmp_path / "other"),)))
        delegation.dispatch_proposal(request, parent, replace(proposal, specialists=(outside, proposal.specialists[1])), context=context)
    with pytest.raises(IntegrationError, match="materially distinct"):
        replace(proposal, specialists=(proposal.specialists[0], replace(
            proposal.specialists[1], prompt=proposal.specialists[0].prompt,
            hypothesis_digest=proposal.specialists[0].hypothesis_digest)))
    with pytest.raises(IntegrationError, match="bind its actual proposed work"):
        replace(proposal.specialists[0], hypothesis_digest=_digest("unrelated prompt"))
    with pytest.raises(WorkerRegistryError, match="may not own files"):
        replace(proposal.specialists[0], contract=WorkerExecutionContract(
            task_scope="compare-answer", speculative_lane=True,
            owned_files=(str(tmp_path / "repo" / "file.py"),)))
    assert registry.get("hyp-1") is None


def test_fan_in_rejects_partial_stale_or_unauthenticated_artifacts(tmp_path):
    _, _, registry, delegation, request, context, _ = _setup(tmp_path)
    parent = _parent(delegation, request, context)
    proposal = _proposal(parent, request.workspace)
    dispatched = delegation.dispatch_proposal(request, parent, proposal, context=context)
    for child in dispatched:
        delegation.integrate(child.request, child.handle.result(timeout=3))
    receipts = _verified_readiness(proposal, dispatched, registry)
    verifier_calls = []

    def track_verifier(record, result):
        verifier_calls.append(record.launch.worker_id)
        return _host_verifier(record, result)

    for partial in (receipts[:1], (receipts[0], receipts[0]),
                    (replace(receipts[0], timestamp=datetime.now(timezone.utc) - timedelta(days=2)), receipts[1]),
                    (replace(receipts[0], content_digest=_digest("fabricated")), receipts[1])):
        with pytest.raises(IntegrationError, match="incomplete or stale"):
            delegation.fan_in_hypotheses(proposal, dispatched,
                                         artifact_readiness=partial, verify_artifact=track_verifier)
        assert verifier_calls == []
    with pytest.raises(IntegrationError, match="independent host verifier"):
        delegation.fan_in_hypotheses(proposal, dispatched, artifact_readiness=receipts)
    with pytest.raises(IntegrationError, match="incomplete or stale"):
        delegation.fan_in_hypotheses(proposal, dispatched, artifact_readiness=receipts,
                                     verify_artifact=lambda record, result: _digest("fabricated"))


def test_fan_in_rejects_relabelled_proposal_and_altered_task_spec(tmp_path):
    _, _, registry, delegation, request, context, _ = _setup(tmp_path)
    parent = _parent(delegation, request, context)
    proposal = _proposal(parent, request.workspace)
    dispatched = delegation.dispatch_proposal(request, parent, proposal, context=context)
    for child in dispatched:
        delegation.integrate(child.request, child.handle.result(timeout=3))
    receipts = _verified_readiness(proposal, dispatched, registry)
    renamed = replace(proposal, proposal_id="unrelated-judgement")
    rebound_receipts = tuple(replace(receipt, run_id=renamed.proposal_id,
                                    artifact_id=f"{renamed.proposal_id}/{receipt.producer_id}")
                           for receipt in receipts)
    with pytest.raises(IntegrationError, match="proposal|lineage"):
        delegation.fan_in_hypotheses(renamed, dispatched, artifact_readiness=rebound_receipts,
                                     verify_artifact=_host_verifier)

    altered = (
        replace(proposal, source_output_digest=_digest("different integrated parent output")),
        replace(proposal, specialists=(replace(proposal.specialists[0], preset="architect"),
                                       proposal.specialists[1])),
        replace(proposal, specialists=(replace(proposal.specialists[0], budget=replace(
            proposal.specialists[0].budget, max_steps=2)), proposal.specialists[1])),
    )
    for forged in altered:
        with pytest.raises(IntegrationError, match="proposal|parent|lineage"):
            delegation.fan_in_hypotheses(forged, dispatched, artifact_readiness=receipts,
                                         verify_artifact=_host_verifier)
    with pytest.raises(IntegrationError, match="persisted lineage"):
        delegation.fan_in_hypotheses(replace(proposal, specialists=proposal.specialists[::-1]),
                                     dispatched, artifact_readiness=receipts,
                                     verify_artifact=_host_verifier)
    with pytest.raises(IntegrationError, match="complete dispatched sibling set"):
        delegation.fan_in_hypotheses(proposal, (dispatched[0], dispatched[0]))


@pytest.mark.parametrize("failed_children", [("hyp-2",), ("hyp-1", "hyp-2")])
def test_fan_in_preserves_failed_hypothesis_diagnostics_and_valid_sibling(
    tmp_path, failed_children,
):
    _, _, registry, delegation, request, context, _ = _setup(tmp_path)
    parent = _parent(delegation, request, context)
    original_factory = delegation._provider._runner_factory

    def fail_selected(child_request, child_context):
        if child_request.child_id in failed_children:
            def fail(*_):
                raise RuntimeError("hypothesis tool exited")
            return fail
        return original_factory(child_request, child_context)

    delegation._provider._runner_factory = fail_selected
    proposal = _proposal(parent, request.workspace)
    dispatched = delegation.dispatch_proposal(request, parent, proposal, context=context)
    for child in dispatched:
        delegation.integrate(child.request, child.handle.result(timeout=3))
    without_verifier = delegation.fan_in_hypotheses(proposal, dispatched)
    assert without_verifier.winning_child_id is None
    assert all(item.conclusion != "supported" for item in without_verifier.results)

    if len(failed_children) == 1:
        verified = _verified_readiness(proposal, dispatched[:1], registry)
        decision = delegation.fan_in_hypotheses(
            proposal, dispatched, artifact_readiness=verified,
            verify_artifact=_host_verifier,
        )
        assert decision.winning_child_id == "hyp-1"
        assert decision.results[0].conclusion == "supported"
        failed_receipt = ArtifactReadiness.from_content(
            "hyp-2", proposal.proposal_id,
            registry.terminal_result("hyp-2").error.message,
            source_revision=_digest(registry.get("hyp-2").launch.prompt),
            verifier_receipt=_digest("untrusted-failure-manifest"),
        )
        with pytest.raises(IntegrationError, match="incomplete or stale"):
            delegation.fan_in_hypotheses(
                proposal, dispatched, artifact_readiness=verified + (failed_receipt,),
                verify_artifact=_host_verifier,
            )
    else:
        decision = without_verifier
        assert "failed" in decision.reason
    failed = [item for item in decision.results if item.child_id in failed_children]
    assert all(item.status is SubagentStatus.FAILED and item.conclusion == "rejected"
               and item.failure_code == "runner_failed" and item.evidence_refs for item in failed)


def test_abandoned_hypothesis_retains_cancellation_evidence_at_fan_in(tmp_path):
    _, _, registry, delegation, request, context, _ = _setup(tmp_path)
    parent = _parent(delegation, request, context)
    original_factory = delegation._provider._runner_factory
    started, release = Event(), Event()

    def wait_for_cancel(child_request, child_context):
        if child_request.child_id == "hyp-2":
            def abandoned(*_):
                started.set()
                assert release.wait(timeout=3)
                return "should not be integrated as success"
            return abandoned
        return original_factory(child_request, child_context)

    delegation._provider._runner_factory = wait_for_cancel
    proposal = _proposal(parent, request.workspace)
    dispatched = delegation.dispatch_proposal(request, parent, proposal, context=context)
    try:
        assert started.wait(timeout=2)
        assert dispatched[1].handle.cancel(reason="hypothesis abandoned")
    finally:
        release.set()
    for child in dispatched:
        delegation.integrate(child.request, child.handle.result(timeout=3))
    verified = _verified_readiness(proposal, dispatched[:1], registry)
    decision = delegation.fan_in_hypotheses(
        proposal, dispatched, artifact_readiness=verified, verify_artifact=_host_verifier,
    )
    assert decision.winning_child_id == "hyp-1"
    loser = next(item for item in decision.results if item.child_id == "hyp-2")
    assert loser.status is SubagentStatus.CANCELLED
    assert loser.conclusion == "rejected" and loser.failure_code == "cancelled"
    assert loser.evidence_refs == (_digest("hypothesis abandoned"),)


def test_host_root_registration_bound_to_immutable_operation_and_owner(tmp_path):
    _, _, registry, delegation, request, context, root_budget = _setup(tmp_path, host_root=True)
    assert registry.get(request.lineage.root_id) is None
    assert delegation.dispatch(request, context).result(timeout=3).child_id == "parent"
    assert registry.get(request.lineage.root_id).launch.owner_id == context.principal_id
    with pytest.raises(IntegrationError, match="host operation identity"):
        delegation.dispatch(replace(request, lineage=replace(request.lineage, root_id="other", parent_id="other")), context)
    other_context = local_owner_context(correlation_id="another-run", workspace_roots=(tmp_path / "repo",))
    with pytest.raises(IntegrationError, match="host operation identity"):
        delegation.dispatch(request, other_context)
    widened = DelegationService(
        delegation._provider, worker_registry=registry,
        host_root_budget=replace(root_budget, max_children=7),
        register_host_root=lambda root, budget, owner: delegation._provider.register_root(
            root, budget, owner_id=owner,
        ),
    )
    with pytest.raises(InvalidSubagentRequest, match="registered root owner or budget differs"):
        widened.dispatch(request, context)


def test_proposal_requires_integrated_proof_and_refuses_depth_three(tmp_path):
    repo, _, _, delegation, request, context, _ = _setup(tmp_path)
    parent = delegation.dispatch(request, context).result(timeout=3)
    proposal = _proposal(parent, request.workspace)
    with pytest.raises(IntegrationError, match="integrated durable authority"):
        delegation.dispatch_proposal(request, parent, proposal, context=context)
    delegation.integrate(request, parent)
    first = delegation.dispatch_proposal(request, parent, proposal, context=context)[0]
    child_result = first.handle.result(timeout=3)
    delegation.integrate(first.request, child_result)
    third_level = replace(proposal, parent_child_id=first.handle.child_id,
                          source_output_digest=ResultEvidence.digest(child_result.output),
                          proposal_id="depth-three",
                          specialists=tuple(replace(item, child_id=f"nested-{index}")
                                            for index, item in enumerate(proposal.specialists)))
    with pytest.raises(IntegrationError, match="recursive depth"):
        delegation.dispatch_proposal(first.request, child_result, third_level, context=context)
    assert repo.get("nested-0") is None


def test_durable_root_and_depth_cannot_be_relabelled_by_a_caller(tmp_path):
    repo, _, _, delegation, request, context, _ = _setup(tmp_path)
    for root_id, depth in (("other-root", 1), ("root", 2)):
        forged = replace(request, delegation_id=f"forged-{depth}-{root_id}", lineage=replace(
            request.lineage, root_id=root_id, depth=depth, child_id=f"forged-child-{depth}-{root_id}"))
        with pytest.raises(IntegrationError, match="durable parent"):
            delegation.dispatch(forged, context)
        assert repo.get(forged.lineage.child_id) is None


def test_partial_admission_retains_running_handle_and_atomic_budget(tmp_path):
    release = Event()
    repo, _, registry, delegation, request, context, _ = _setup(tmp_path, hold_child=release)
    parent = _parent(delegation, request, context)
    proposal = _proposal(parent, request.workspace,
                         budget=SubagentBudget(max_steps=4, max_output_tokens=400, max_wall_seconds=20))
    try:
        with pytest.raises(PartialDelegationError) as failure:
            delegation.dispatch_proposal(request, parent, proposal, context=context)
        assert len(failure.value.dispatched) == 1
        assert failure.value.dispatched[0].handle.child_id == "hyp-1"
        assert repo.get("hyp-1") is not None
        assert registry.get("hyp-2") is None
    finally:
        release.set()
    assert failure.value.dispatched[0].handle.result(timeout=3).child_id == "hyp-1"


def test_same_owned_task_refused_without_speculative_identity(tmp_path):
    release = Event()
    repo, _, _, delegation, request, context, _ = _setup(tmp_path, hold_child=release)
    parent = _parent(delegation, request, context)
    template = _proposal(parent, request.workspace)
    owned = tuple(replace(item, contract=WorkerExecutionContract(task_scope="one-owned-question"),
                          hypothesis_digest="", speculative_lane_id="") for item in template.specialists)
    proposal = replace(template, specialists=owned)
    try:
        with pytest.raises(PartialDelegationError) as failure:
            delegation.dispatch_proposal(request, parent, proposal, context=context)
        assert len(failure.value.dispatched) == 1
        assert repo.get("hyp-2") is None
    finally:
        release.set()
    assert failure.value.dispatched[0].handle.result(timeout=3).child_id == "hyp-1"


def test_composed_application_registers_operation_root_before_delegation(tmp_path, monkeypatch):
    from sonder_runtime.adapters import conversational_subagents
    from sonder_runtime.bootstrap.app import build_application
    from sonder_runtime.platform.config import SonderConfig

    monkeypatch.setattr(
        conversational_subagents, "conversational_runner_factory",
        lambda *_: lambda request, context: lambda state, save, control: "finished",
    )
    config = SonderConfig()
    config = replace(config, state=replace(
        config.state, home=str(tmp_path), workspace_roots=(str(tmp_path),
        )))
    application = build_application(config=config)
    try:
        delegation = application.delegation_service()
        context = local_owner_context(correlation_id="composed-operation", workspace_roots=(tmp_path,))
        root_id = delegation.root_id_for_context(context)
        preset = resolve_preset("researcher")
        workspace = WorkspaceAssignment((str(tmp_path),))
        lineage = LineageRecord("composed-lineage", root_id, root_id,
                                "composed-child", 1, preset.name, preset.role, workspace)
        request = DelegationRequest("composed-delegation", lineage, "inspect", preset, workspace)
        result = delegation.dispatch(request, context).result(timeout=3)
        assert result.child_id == "composed-child"
        assert delegation._worker_registry.get(root_id).launch.owner_id == context.principal_id
    finally:
        application.close_delegation()
