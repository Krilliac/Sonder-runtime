import pytest

from sonder_runtime.adapters.persistence.durable_continuation import SQLiteDurableContinuationRepository
from sonder_runtime.adapters.subagents import RunnerBoundSubagentProvider
from sonder_runtime.application.agents.delegation_service import DelegationService
from sonder_runtime.application.agents.lineage_delegation import (
    DelegationRequest, LineageRecord, WorkspaceAssignment,
)
from sonder_runtime.application.agents.presets import resolve_preset
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports.continuation_records import ChildSessionLineage, DurableChildSession
from sonder_runtime.application.ports.subagents import (
    InvalidSubagentRequest, SubagentBudget, SubagentRequest,
)
from sonder_runtime.application.ports.worker_registry import DuplicateWorkerError, WorkerLaunch, WorkerStatus
from sonder_runtime.application.subagents.durable_continuation import DurableContinuationService
from sonder_runtime.application.worker_registry.continuation import ContinuationWorkerRegistry


def _launch(root, *, worker_id="child-1", owner="owner"):
    return WorkerLaunch(
        worker_id, "root-1", "researcher", "local-provider", "subagent-provider", "default",
        (str(root),), ("research",),
        {"max_steps": 8, "max_output_tokens": 4000, "max_wall_seconds": 30},
        {"max_attempts": 1}, "delegation-1", "delegation-1",
        "research the change", owner,
    )


def _request_for_reservation(launch):
    return SubagentRequest(
        launch.parent_id,
        launch.prompt,
        SubagentBudget(max_steps=8, max_output_tokens=4000, max_wall_seconds=30),
        launch.worker_id,
        (
            ("worker_registry_admitted", "true"),
            ("worker_role", launch.role),
            ("model", launch.model),
            ("backend", launch.backend),
            ("effort", launch.effort),
            ("scope", "|".join(launch.scope)),
            ("allowed_tools", "|".join(launch.allowed_tools)),
            ("owner_id", launch.owner_id),
            ("worker_id", launch.worker_id),
            ("retry_max_attempts", "1"),
        ),
        launch.resume_key,
        launch.idempotency_key,
    )


def test_registry_reserves_in_the_continuation_store_and_provider_consumes_it(tmp_path):
    repository = SQLiteDurableContinuationRepository(tmp_path / "continuation.sqlite")
    repository.create(DurableChildSession(
        SubagentRequest("root-1", "provider root", SubagentBudget(max_steps=8, max_output_tokens=4000, max_wall_seconds=30), "root-1", (("provider_root", "true"),)),
        ChildSessionLineage("root-1"),
    ))
    registry = ContinuationWorkerRegistry(repository)
    launch = _launch(tmp_path / "repo")
    admitted = registry.admit(launch)
    assert admitted.status is WorkerStatus.QUEUED
    with pytest.raises(DuplicateWorkerError):
        registry.admit(_launch(tmp_path / "repo", worker_id="other"))

    service = DurableContinuationService(repository)
    handle = service.spawn(
        _request_for_reservation(launch),
        local_owner_context(correlation_id="delegation-1", workspace_roots=(tmp_path / "repo",)),
        lambda state, save, cancellation: "complete",
    )
    result = handle.result(timeout=5)
    assert result.output == "complete"
    record = registry.get(launch.worker_id)
    assert record is not None
    assert record.status is WorkerStatus.SUCCEEDED
    assert record.launch.owner_id == "owner"


def test_reserved_worker_cannot_be_claimed_by_another_owner(tmp_path):
    repository = SQLiteDurableContinuationRepository(tmp_path / "continuation.sqlite")
    root = SubagentRequest("root-1", "provider root", SubagentBudget(max_steps=8), "root-1", (("provider_root", "true"),))
    repository.create(DurableChildSession(root, ChildSessionLineage("root-1")))
    launch = _launch(tmp_path / "repo")
    ContinuationWorkerRegistry(repository).admit(launch)
    request = _request_for_reservation(launch)
    context = local_owner_context(correlation_id="other", workspace_roots=(tmp_path / "repo",))
    object.__setattr__(context, "principal_id", "different-owner")
    with pytest.raises(InvalidSubagentRequest, match="another owner"):
        DurableContinuationService(repository).spawn(request, context, lambda *_: "must not run")


def test_delegation_dispatch_admits_through_injected_registry(tmp_path):
    class Handle:
        child_id = "child-1"
        parent_id = "parent-1"

    class Provider:
        def spawn(self, request, context):
            return Handle()

    class Registry:
        def __init__(self):
            self.launches = []

        def admit(self, launch):
            self.launches.append(launch)
            return None

    root = tmp_path / "repo"
    workspace = WorkspaceAssignment((str(root),), (str(root / "write"),))
    preset = resolve_preset("researcher")
    lineage = LineageRecord("line-1", "root-1", "parent-1", "child-1", 1, preset.name, preset.role, workspace)
    request = DelegationRequest("delegation-1", lineage, "research the change", preset, workspace)
    registry = Registry()
    DelegationService(Provider(), worker_registry=registry).dispatch(
        request,
        local_owner_context(correlation_id="delegation-1", workspace_roots=(root,)),
    )
    assert len(registry.launches) == 1
    assert registry.launches[0].resume_key == request.delegation_id
    assert registry.launches[0].owner_id == "owner"


def test_delegation_service_consumes_continuation_backed_reservation(tmp_path):
    repository = SQLiteDurableContinuationRepository(tmp_path / "continuation.sqlite")
    service = DurableContinuationService(repository)
    provider = RunnerBoundSubagentProvider(
        service,
        lambda state, save, cancellation: "delegated output",
    )
    registry = ContinuationWorkerRegistry(repository)
    root = tmp_path / "repo"
    workspace = WorkspaceAssignment((str(root),), (str(root / "write"),))
    preset = resolve_preset("researcher")
    lineage = LineageRecord("line-1", "root-1", "parent-1", "child-1", 1, preset.name, preset.role, workspace)
    request = DelegationRequest("delegation-1", lineage, "research the change", preset, workspace)
    repository.create(DurableChildSession(
        SubagentRequest("parent-1", "provider root", SubagentBudget(max_steps=30, max_output_tokens=6000, max_wall_seconds=600), "parent-1", (("provider_root", "true"),)),
        ChildSessionLineage("parent-1"),
    ))
    handle = DelegationService(provider, worker_registry=registry).dispatch(
        request,
        local_owner_context(correlation_id="delegation-1", workspace_roots=(root,)),
    )
    assert handle.result(timeout=5).output == "delegated output"
    record = registry.get("child-1")
    assert record is not None and record.status is WorkerStatus.SUCCEEDED
