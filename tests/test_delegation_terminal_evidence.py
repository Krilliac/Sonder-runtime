"""Delegated evidence must describe the durable child, even after restart."""

import hashlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event

import pytest

from sonder_runtime.adapters.persistence.durable_continuation import (
    SQLiteDurableContinuationRepository,
)
from sonder_runtime.adapters.subagents import (
    LocalSubagentProvider,
    RunnerBoundSubagentProvider,
)
from sonder_runtime.application.agents.delegation_service import DelegationService
from sonder_runtime.application.agents.lineage_delegation import (
    DelegationRequest,
    IntegrationError,
    LineageRecord,
    WorkspaceAssignment,
)
from sonder_runtime.application.agents.presets import resolve_preset
from sonder_runtime.application.agents.workflow_integration import AgentWorkflowService
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports.continuation_mutations import (
    ContinuationStorageFailure,
)
from sonder_runtime.application.ports.subagents import (
    SubagentBudget,
    SubagentError,
    SubagentResult,
    SubagentStatus,
    SubagentUsage,
)
from sonder_runtime.application.ports.worker_registry import WorkerRegistryError
from sonder_runtime.application.subagents.durable_continuation import (
    DurableContinuationService,
)
from sonder_runtime.application.worker_registry.continuation import (
    ContinuationWorkerRegistry,
)
from sonder_runtime.domain.agents.roles import AgentRole


def _delegation(tmp_path, runner):
    database = tmp_path / "children.sqlite"
    repo = SQLiteDurableContinuationRepository(database)
    service = DurableContinuationService(repo)
    service.register_root(
        "root", SubagentBudget(max_steps=30, max_output_tokens=8000, max_wall_seconds=900)
    )
    registry = ContinuationWorkerRegistry(repo)
    delegation = DelegationService(RunnerBoundSubagentProvider(service, runner), worker_registry=registry)
    workspace = WorkspaceAssignment((str(tmp_path / "repo"),), ())
    preset = resolve_preset("researcher")
    lineage = LineageRecord(
        "line-1", "root", "root", "child-1", 1,
        preset.name, preset.role, workspace,
    )
    request = DelegationRequest("delegation-1", lineage, "do the work", preset, workspace)
    context = local_owner_context(correlation_id="delegation-1", workspace_roots=(tmp_path / "repo",))
    return repo, registry, delegation, request, context


@pytest.mark.parametrize("tamper", ("status", "output", "usage"))
def test_integration_rejects_result_that_disagrees_with_durable_child(tmp_path, tamper):
    repo, registry, delegation, request, context = _delegation(
        tmp_path, lambda state, save, control: "actual output"
    )
    actual = delegation.dispatch(request, context).result(timeout=2)
    if tamper == "status":
        forged = SubagentResult(
            actual.child_id, actual.parent_id, SubagentStatus.FAILED,
            error=SubagentError("runner_failed", "fabricated failure"), usage=actual.usage,
        )
    elif tamper == "output":
        forged = replace(actual, output="fabricated output")
    else:
        forged = replace(actual, usage=SubagentUsage(steps=actual.usage.steps + 1))

    with pytest.raises(IntegrationError, match="durable terminal result"):
        delegation.integrate(request, forged, verification=("review passed",))
    assert repo.get(actual.child_id).terminal_verification == {}
    assert registry.terminal_result(actual.child_id) == actual


def test_integration_rejects_forged_failure_error(tmp_path):
    repo, registry, delegation, request, context = _delegation(
        tmp_path, lambda state, save, control: (_ for _ in ()).throw(RuntimeError("actual failure"))
    )
    actual = delegation.dispatch(request, context).result(timeout=2)
    assert actual.status is SubagentStatus.FAILED
    with pytest.raises(IntegrationError, match="durable terminal result"):
        delegation.integrate(request, replace(actual, error=SubagentError("runner_failed", "invented")))
    with pytest.raises(IntegrationError, match="durable terminal result"):
        delegation.integrate(request, replace(actual, status=SubagentStatus.SUCCEEDED, error=None, output="invented"))
    assert repo.get(actual.child_id).terminal_verification == {}
    assert registry.terminal_result(actual.child_id) == actual


def test_integration_refuses_a_result_before_child_is_terminal(tmp_path):
    started, release = Event(), Event()

    def hold(state, save, control):
        started.set()
        assert release.wait(2)
        return "eventual output"

    repo, registry, delegation, request, context = _delegation(tmp_path, hold)
    handle = delegation.dispatch(request, context)
    try:
        assert started.wait(1)
        alleged = SubagentResult("child-1", "root", SubagentStatus.SUCCEEDED, output="eventual output")
        with pytest.raises(IntegrationError, match="durable terminal result"):
            delegation.integrate(request, alleged)
        assert registry.terminal_result(handle.child_id) is None
        assert repo.get(handle.child_id).terminal_verification == {}
    finally:
        release.set()
        assert handle.result(2).status is SubagentStatus.SUCCEEDED


def test_registry_refuses_forged_receipt_and_immutable_conflicting_evidence(tmp_path):
    repo, registry, delegation, request, context = _delegation(
        tmp_path, lambda state, save, control: "actual output"
    )
    result = delegation.dispatch(request, context).result(timeout=2)
    revision = registry.get(result.child_id).revision
    digest = hashlib.sha256(result.output.encode("utf-8")).hexdigest()
    verified = {"status": "succeeded", "output_digest": digest,
                "usage_steps": result.usage.steps, "verification": ("checked",)}
    for value in (
        {**verified, "output_digest": hashlib.sha256(b"invented").hexdigest()},
        {**verified, "status": "failed"},
        {**verified, "usage_steps": result.usage.steps + 1},
        {**verified, "output": "invented output"},
        {**verified, "error": "invented failure"},
    ):
        with pytest.raises(WorkerRegistryError, match="terminal result"):
            registry.record_verification(result.child_id, value, expected_revision=revision)
    with pytest.raises(WorkerRegistryError, match="execution contract"):
        registry.record_verification(
            result.child_id, {**verified, "success_criteria": ("fabricated",)},
            expected_revision=revision,
        )
    assert repo.get(result.child_id).terminal_verification == {}

    recorded = registry.record_verification(result.child_id, verified, expected_revision=revision)
    assert recorded is not None
    with pytest.raises(WorkerRegistryError, match="already recorded"):
        registry.record_verification(
            result.child_id, {**verified, "verification": ("different",)},
            expected_revision=recorded.revision,
        )
    assert registry.record_verification(result.child_id, verified, expected_revision=recorded.revision) == recorded
    reopened = SQLiteDurableContinuationRepository(tmp_path / "children.sqlite")
    assert reopened.get(result.child_id).terminal_verification["verification"] == ["checked"]


def test_matching_integration_reuses_one_terminal_receipt(tmp_path):
    repo, registry, delegation, request, context = _delegation(
        tmp_path, lambda state, save, control: "actual output"
    )
    result = delegation.dispatch(request, context).result(timeout=2)
    first = delegation.integrate(request, result, verification=("review passed",))
    revision = registry.get(result.child_id).revision
    again = delegation.integrate(request, result, verification=("review passed",))
    assert again.evidence == first.evidence
    assert registry.get(result.child_id).revision == revision
    with pytest.raises(WorkerRegistryError, match="already recorded"):
        delegation.integrate(request, result, verification=("other review",))
    assert repo.get(result.child_id).terminal_verification["verification"] == ["review passed"]


def test_resumed_child_integrates_only_its_latest_terminal_result(tmp_path):
    def failed(_state, _save, _control):
        raise RuntimeError("first attempt failed")

    repo, registry, delegation, request, context = _delegation(tmp_path, failed)
    original = delegation.dispatch(request, context).result(timeout=2)
    assert original.status is SubagentStatus.FAILED
    delegation.integrate(request, original, verification=("first attempt reviewed",))
    started, release = Event(), Event()

    def recovered(_state, _save, _control):
        started.set()
        assert release.wait(2)
        return "successful retry"

    service = DurableContinuationService(repo)
    handle = service.resume(original.child_id, context, recovered)
    try:
        assert started.wait(1)
        assert repo.get(original.child_id).terminal_verification == {}
        assert registry.terminal_result(original.child_id) is None
    finally:
        release.set()
    latest = handle.result(timeout=2)
    with pytest.raises(IntegrationError, match="durable terminal result"):
        delegation.integrate(request, original)
    integrated = delegation.integrate(request, latest, verification=("retry reviewed",))
    assert integrated.evidence.status.value == "succeeded"
    assert repo.get(latest.child_id).terminal_verification["verification"] == ["retry reviewed"]


def test_workflow_cannot_advance_on_forged_success_for_failed_child(tmp_path):
    repo = SQLiteDurableContinuationRepository(tmp_path / "workflow.sqlite")
    service = DurableContinuationService(repo)
    service.register_root("root", SubagentBudget(max_steps=30, max_output_tokens=8000, max_wall_seconds=900))

    def fail(state, save, control):
        raise RuntimeError("actual worker failure")

    delegation = DelegationService(
        RunnerBoundSubagentProvider(service, fail), worker_registry=ContinuationWorkerRegistry(repo)
    )
    workflow = AgentWorkflowService(delegation, roles=(AgentRole.EXPLORER, AgentRole.ARCHITECT))
    root = tmp_path / "repo"
    context = local_owner_context(correlation_id="workflow", workspace_roots=(root,))
    dispatch = workflow.start(
        workflow_id="wf", root_id="root", parent_id="root", prompt="inspect",
        workspace=WorkspaceAssignment((str(root),), ()), context=context,
    )
    actual = dispatch.handle.result(timeout=2)
    assert actual.status is SubagentStatus.FAILED
    forged = SubagentResult(actual.child_id, actual.parent_id, SubagentStatus.SUCCEEDED,
                            output="false success", usage=actual.usage)
    with pytest.raises(IntegrationError, match="durable terminal result"):
        workflow.advance(dispatch, forged, context=context)
    assert repo.get("wf:2:architect") is None
    assert repo.get(actual.child_id).terminal_verification == {}


def test_failed_provider_factory_releases_only_its_unstarted_reservation(tmp_path):
    repo, registry, _, request, context = _delegation(tmp_path, lambda *_: "unused")

    def refuse(_request, _context):
        raise RuntimeError("runner factory unavailable")

    provider = LocalSubagentProvider(DurableContinuationService(repo), runner_factory=refuse)
    delegation = DelegationService(provider, worker_registry=registry)
    with pytest.raises(RuntimeError, match="runner factory unavailable"):
        delegation.dispatch(request, context)
    record = repo.get(request.lineage.child_id)
    assert record.status is SubagentStatus.CANCELLED
    assert record.result is not None and record.result.status is SubagentStatus.CANCELLED
    assert registry.terminal_result(record.request.child_id) == record.result


def test_failed_duplicate_dispatch_preserves_original_unstarted_reservation(tmp_path):
    repo, registry, _, request, context = _delegation(tmp_path, lambda *_: "unused")
    service = DurableContinuationService(repo)
    factory_entered, release_factory = Event(), Event()

    def first_factory(_request, _context):
        factory_entered.set()
        assert release_factory.wait(2)
        return lambda _state, _save, _control: "original worker completed"

    def second_factory(_request, _context):
        raise RuntimeError("duplicate launch failed")

    first = DelegationService(
        LocalSubagentProvider(service, runner_factory=first_factory), worker_registry=registry
    )
    second = DelegationService(
        LocalSubagentProvider(service, runner_factory=second_factory), worker_registry=registry
    )
    with ThreadPoolExecutor(max_workers=1) as executor:
        handle = executor.submit(first.dispatch, request, context)
        try:
            assert factory_entered.wait(1)
            with pytest.raises(RuntimeError, match="duplicate launch failed"):
                second.dispatch(request, context)
            assert repo.get(request.lineage.child_id).status is SubagentStatus.CREATED
        finally:
            release_factory.set()
        assert handle.result(timeout=2).result(timeout=2).output == "original worker completed"


def test_uncertain_provider_launch_does_not_cancel_unproven_reservation(tmp_path):
    repo, registry, _, request, context = _delegation(tmp_path, lambda *_: "unused")

    class UncertainProvider(LocalSubagentProvider):
        cancelled = False

        def spawn(self, child_request, child_context):
            raise ContinuationStorageFailure("uncertain provider outcome")

        def cancel_unstarted(self, child_id, *, expected_revision, reason="cancellation requested"):
            self.cancelled = True
            return super().cancel_unstarted(child_id, expected_revision=expected_revision, reason=reason)

    provider = UncertainProvider(DurableContinuationService(repo), runner=lambda *_: "unused")
    with pytest.raises(ContinuationStorageFailure, match="uncertain provider outcome"):
        DelegationService(provider, worker_registry=registry).dispatch(request, context)
    assert not provider.cancelled
    assert repo.get(request.lineage.child_id).status is SubagentStatus.CREATED


def test_provider_failure_after_start_does_not_cancel_running_child(tmp_path):
    repo, registry, _, request, context = _delegation(tmp_path, lambda *_: "unused")
    started, release = Event(), Event()

    def hold(_state, _save, _control):
        started.set()
        assert release.wait(2)
        return "completed despite launcher diagnostic"

    class StartedThenRaises(LocalSubagentProvider):
        def spawn(self, child_request, child_context):
            super().spawn(child_request, child_context)
            assert started.wait(1)
            raise RuntimeError("postlaunch diagnostic")

    local_service = DurableContinuationService(repo)
    provider = StartedThenRaises(local_service, hold)
    delegation = DelegationService(provider, worker_registry=registry)
    try:
        with pytest.raises(RuntimeError, match="postlaunch diagnostic"):
            delegation.dispatch(request, context)
        record = repo.get(request.lineage.child_id)
        assert record.status is SubagentStatus.RUNNING
        assert not record.cancellation_requested
    finally:
        release.set()
        assert local_service.result(request.lineage.child_id, timeout=2).status is SubagentStatus.SUCCEEDED
