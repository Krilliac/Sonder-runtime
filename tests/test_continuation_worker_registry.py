import pytest
import os
import platform
from dataclasses import replace

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
    SubagentResult, SubagentStatus, SubagentUsage,
)
from sonder_runtime.application.ports.worker_registry import DuplicateWorkerError, WorkerLaunch, WorkerRegistryError, WorkerStatus
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
        repository.get("child-1").request,
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


def test_dead_owner_reservation_can_be_reclaimed_but_live_owner_cannot(tmp_path):
    repository = SQLiteDurableContinuationRepository(tmp_path / "continuation.sqlite")
    root = SubagentRequest("root-1", "provider root", SubagentBudget(max_steps=8), "root-1", (("provider_root", "true"),))
    repository.create(DurableChildSession(root, ChildSessionLineage("root-1")))
    launch = replace(
        _launch(tmp_path / "repo"),
        metadata=(
            ("worker_registry_admitted", "true"),
            ("worker_role", "researcher"),
            ("model", "local-provider"),
            ("backend", "subagent-provider"),
            ("effort", "default"),
            ("scope", str(tmp_path / "repo")),
            ("allowed_tools", "research"),
            ("owner_id", "owner"),
            ("worker_id", "child-1"),
            ("retry_max_attempts", "1"),
            ("owner_nonce", "dead-owner"),
            ("owner_pid", "99999999"),
            ("owner_host", platform.node()),
        ),
    )
    ContinuationWorkerRegistry(
        repository, owner_nonce="dead-owner", owner_pid=99999999, owner_host=platform.node()
    ).admit(launch)
    current_values = dict(launch.metadata)
    current_values.update({
        "owner_nonce": "new-owner",
        "owner_pid": str(os.getpid()),
        "owner_host": platform.node(),
    })
    current = replace(launch, metadata=tuple(current_values.items()))
    new_registry = ContinuationWorkerRegistry(
        repository, owner_nonce="new-owner", owner_pid=os.getpid(), owner_host=platform.node()
    )
    reclaimed = new_registry.admit(current)
    assert reclaimed.launch.metadata == launch.metadata
    service = DurableContinuationService(repository)
    handle = service.spawn(
        repository.get("child-1").request,
        local_owner_context(correlation_id="reclaim", workspace_roots=(tmp_path / "repo",)),
        lambda state, save, cancellation: "reclaimed",
    )
    assert handle.result(timeout=5).output == "reclaimed"

    live = replace(
        launch,
        worker_id="child-live",
        resume_key="delegation-live",
        idempotency_key="delegation-live",
        metadata=tuple(
            (key, "live-owner" if key == "owner_nonce" else str(os.getpid()) if key == "owner_pid" else value)
            for key, value in launch.metadata
        ),
    )
    ContinuationWorkerRegistry(
        repository, owner_nonce="live-owner", owner_pid=os.getpid(), owner_host=platform.node()
    ).admit(live)
    live_claim = replace(live, metadata=tuple(
        ("owner_nonce", "other-owner") if key == "owner_nonce" else (key, value)
        for key, value in live.metadata
    ))
    with pytest.raises((DuplicateWorkerError, InvalidSubagentRequest, WorkerRegistryError)):
        ContinuationWorkerRegistry(
            repository, owner_nonce="other-owner", owner_pid=os.getpid(), owner_host=platform.node()
        ).admit(live_claim)


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
    delegation = DelegationService(provider, worker_registry=registry)
    handle = delegation.dispatch(
        request,
        local_owner_context(correlation_id="delegation-1", workspace_roots=(root,)),
    )
    assert handle.result(timeout=5).output == "delegated output"
    delegation.integrate(
        request,
        SubagentResult(
            "child-1", "parent-1", SubagentStatus.SUCCEEDED,
            output="delegated output",
            usage=SubagentUsage(steps=1),
        ),
        verification=("tests passed",),
        artifacts=("evidence.json",),
    )
    record = registry.get("child-1")
    assert record is not None and record.status is WorkerStatus.SUCCEEDED
    assert record.terminal_verification["verification"] == ("tests passed",)
    reopened = ContinuationWorkerRegistry(
        SQLiteDurableContinuationRepository(tmp_path / "continuation.sqlite")
    ).get("child-1")
    assert reopened is not None
    assert reopened.terminal_verification["artifacts"] == ("evidence.json",)
