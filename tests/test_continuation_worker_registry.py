import os
import platform
from dataclasses import replace

import pytest

from sonder_runtime.adapters.persistence.durable_continuation import (
    SQLiteDurableContinuationRepository,
)
from sonder_runtime.adapters.subagents import RunnerBoundSubagentProvider
from sonder_runtime.application.agents.delegation_service import DelegationService
from sonder_runtime.application.agents.lineage_delegation import (
    DelegationRequest,
    IntegrationError,
    LineageRecord,
    WorkspaceAssignment,
)
from sonder_runtime.application.agents.presets import resolve_preset
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports.continuation_records import (
    ChildSessionLineage,
    DurableChildSession,
)
from sonder_runtime.application.ports.subagents import (
    InvalidSubagentRequest,
    SubagentBudget,
    SubagentRequest,
    SubagentResult,
    SubagentStatus,
    SubagentUsage,
)
from sonder_runtime.application.ports.worker_registry import (
    DuplicateWorkerError,
    WorkerContextInput,
    WorkerContextPolicy,
    WorkerExecutionContract,
    WorkerLaunch,
    WorkerRegistryError,
    WorkerStatus,
    owned_paths_overlap,
)
from sonder_runtime.application.subagents.durable_continuation import (
    DurableContinuationService,
)
from sonder_runtime.application.worker_registry.continuation import (
    ContinuationWorkerRegistry,
)


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


def test_completed_child_is_reused_after_service_restart_before_spawn(tmp_path):
    repository = SQLiteDurableContinuationRepository(tmp_path / "continuation.sqlite")
    root_request = SubagentRequest(
        "root-1", "provider root", SubagentBudget(max_steps=8), "root-1",
        (("provider_root", "true"),),
    )
    repository.create(DurableChildSession(root_request, ChildSessionLineage("root-1")))
    launch = _launch(tmp_path / "repo")
    request = _request_for_reservation(launch)
    context = local_owner_context(
        correlation_id="delegation-1", workspace_roots=(tmp_path / "repo",)
    )
    object.__setattr__(context, "principal_id", "owner")
    values = dict(request.metadata)
    values.update({
        "context_workspace_roots": str(tmp_path / "repo"),
        "context_cloud_allowed": str(context.cloud_allowed),
        "context_remote_ollama_allowed": str(context.remote_ollama_allowed),
        "context_session_id": str(context.session_id),
    })
    request = replace(request, metadata=tuple(values.items()))
    calls = []

    def runner(state, save, cancellation):
        calls.append(1)
        return "persisted result"

    first = DurableContinuationService(repository)
    first_handle = first.spawn(request, context, runner)
    assert first_handle.result(timeout=5).output == "persisted result"
    first.close(timeout=1)

    restarted = DurableContinuationService(repository)
    reused = restarted.spawn(request, context, lambda *_: calls.append(2) or "wrong")
    assert reused.child_id == launch.worker_id
    assert reused.result(timeout=1).output == "persisted result"
    assert calls == [1]


def test_terminal_child_scope_mismatch_cannot_be_reused_or_respawned(tmp_path):
    repository = SQLiteDurableContinuationRepository(tmp_path / "continuation.sqlite")
    root_request = SubagentRequest(
        "root-1", "provider root", SubagentBudget(max_steps=8), "root-1",
        (("provider_root", "true"),),
    )
    repository.create(DurableChildSession(root_request, ChildSessionLineage("root-1")))
    launch = _launch(tmp_path / "repo")
    request = _request_for_reservation(launch)
    context = local_owner_context(
        correlation_id="delegation-1", workspace_roots=(tmp_path / "repo",)
    )
    object.__setattr__(context, "principal_id", "owner")
    values = dict(request.metadata)
    values.update({
        "context_workspace_roots": str(tmp_path / "repo"),
        "context_cloud_allowed": str(context.cloud_allowed),
        "context_remote_ollama_allowed": str(context.remote_ollama_allowed),
        "context_session_id": str(context.session_id),
    })
    request = replace(request, metadata=tuple(values.items()))
    service = DurableContinuationService(repository)
    assert service.spawn(request, context, lambda *_: "done").result(timeout=5).output == "done"
    mismatched = replace(request, prompt="changed prompt")
    with pytest.raises(InvalidSubagentRequest, match="terminal child identity"):
        DurableContinuationService(repository).spawn(
            mismatched, context, lambda *_: pytest.fail("runner must not run")
        )


def test_terminal_reuse_rejects_different_operation_context_owner(tmp_path):
    repository = SQLiteDurableContinuationRepository(tmp_path / "owner.sqlite")
    root_request = SubagentRequest(
        "root-1", "provider root", SubagentBudget(max_steps=8), "root-1",
        (("provider_root", "true"),),
    )
    repository.create(DurableChildSession(root_request, ChildSessionLineage("root-1")))
    launch = _launch(tmp_path / "repo")
    request = _request_for_reservation(launch)
    context = local_owner_context(correlation_id="delegation-1", workspace_roots=(tmp_path / "repo",))
    object.__setattr__(context, "principal_id", "owner")
    values = dict(request.metadata)
    values.update({
        "context_workspace_roots": str(tmp_path / "repo"),
        "context_cloud_allowed": str(context.cloud_allowed),
        "context_remote_ollama_allowed": str(context.remote_ollama_allowed),
        "context_session_id": str(context.session_id),
    })
    request = replace(request, metadata=tuple(values.items()))
    assert DurableContinuationService(repository).spawn(
        request, context, lambda *_: "done"
    ).result(timeout=5).output == "done"
    foreign = local_owner_context(correlation_id="foreign", workspace_roots=(tmp_path / "repo",))
    object.__setattr__(foreign, "principal_id", "foreign-owner")
    with pytest.raises(InvalidSubagentRequest, match="operation scope cannot be proven"):
        DurableContinuationService(repository).spawn(request, foreign, lambda *_: pytest.fail("runner must not run"))


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
    result = handle.result(timeout=5)
    assert result.output == "delegated output"
    delegation.integrate(
        request,
        result,
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


def test_delegation_restart_reuses_terminal_by_key_with_new_child_id(tmp_path):
    database = tmp_path / "continuation.sqlite"
    root = tmp_path / "repo"
    preset = resolve_preset("researcher")
    workspace = WorkspaceAssignment((str(root),), (str(root / "write"),))
    lineage = LineageRecord("line-1", "root-1", "parent-1", "child-1", 1, preset.name, preset.role, workspace)
    request = DelegationRequest("delegation-1", lineage, "research the change", preset, workspace)
    root_request = SubagentRequest(
        "parent-1", "provider root", SubagentBudget(max_steps=30, max_output_tokens=6000, max_wall_seconds=600),
        "parent-1", (("provider_root", "true"),),
    )
    repository = SQLiteDurableContinuationRepository(database)
    repository.create(DurableChildSession(root_request, ChildSessionLineage("parent-1")))
    calls = []
    first_service = DurableContinuationService(repository)
    first_provider = RunnerBoundSubagentProvider(
        first_service, lambda state, save, cancellation: calls.append("first") or "persisted"
    )
    first = DelegationService(first_provider, worker_registry=ContinuationWorkerRegistry(repository))
    context = local_owner_context(correlation_id="delegation-1", workspace_roots=(root,))
    result = first.dispatch(request, context).result(timeout=5)
    assert result.output == "persisted"
    first.integrate(request, result)
    first_service.close(timeout=1)

    restarted_repository = SQLiteDurableContinuationRepository(database)
    restarted_service = DurableContinuationService(restarted_repository)
    second_calls = []
    second_provider = RunnerBoundSubagentProvider(
        restarted_service, lambda state, save, cancellation: second_calls.append("spawned") or "wrong"
    )
    second = DelegationService(second_provider, worker_registry=ContinuationWorkerRegistry(restarted_repository))
    retry = replace(request, lineage=replace(lineage, child_id="child-2"))
    reused = second.dispatch(retry, context)
    assert reused.child_id == "child-1"
    assert reused.result(timeout=1).output == "persisted"
    assert calls == ["first"]
    assert second_calls == []


def test_execution_contract_is_durable_and_integration_is_a_fail_closed_gate(tmp_path):
    database = tmp_path / "contract.sqlite"
    root = tmp_path / "repo"
    preset = resolve_preset("researcher")
    workspace = WorkspaceAssignment((str(root),), (str(root / "write"),))
    contract = WorkerExecutionContract(
        success_criteria=("tests pass", "report emitted"),
        verification_commands=(("python", "-m", "pytest", "tests/test_target.py"),),
    )
    lineage = LineageRecord("line-1", "root-1", "parent-1", "child-1", 1, preset.name, preset.role, workspace)
    request = DelegationRequest("delegation-1", lineage, "research the change", preset, workspace, execution_contract=contract)
    repository = SQLiteDurableContinuationRepository(database)
    repository.create(DurableChildSession(
        SubagentRequest("parent-1", "provider root", SubagentBudget(max_steps=30), "parent-1", (("provider_root", "true"),)),
        ChildSessionLineage("parent-1"),
    ))
    continuation = DurableContinuationService(repository)
    provider = RunnerBoundSubagentProvider(continuation, lambda state, save, cancellation: "persisted")
    service = DelegationService(provider, worker_registry=ContinuationWorkerRegistry(repository))
    context = local_owner_context(correlation_id="delegation-1", workspace_roots=(root,))
    result = service.dispatch(request, context).result(timeout=5)
    assert result.output == "persisted"
    with pytest.raises(IntegrationError, match="criteria"):
        service.integrate(request, result, verification=("tests pass",), verification_commands=contract.verification_commands)
    service.integrate(
        request,
        result,
        verification=contract.success_criteria,
        verification_commands=contract.verification_commands,
    )
    reopened = ContinuationWorkerRegistry(SQLiteDurableContinuationRepository(database)).get("child-1")
    assert reopened is not None
    assert reopened.launch.execution_contract == contract
    assert reopened.terminal_verification["verification_commands"] == contract.verification_commands


def test_execution_contract_command_mismatch_cannot_certify_restarted_child(tmp_path):
    database = tmp_path / "contract-mismatch.sqlite"
    root = tmp_path / "repo"
    preset = resolve_preset("researcher")
    workspace = WorkspaceAssignment((str(root),), ())
    contract = WorkerExecutionContract(("tests pass",), (("python", "-m", "pytest", "tests/test_target.py"),))
    lineage = LineageRecord("line-1", "root-1", "parent-1", "child-1", 1, preset.name, preset.role, workspace)
    request = DelegationRequest("delegation-1", lineage, "research", preset, workspace, execution_contract=contract)
    repository = SQLiteDurableContinuationRepository(database)
    repository.create(DurableChildSession(
        SubagentRequest("parent-1", "provider root", SubagentBudget(max_steps=30), "parent-1", (("provider_root", "true"),)),
        ChildSessionLineage("parent-1"),
    ))
    first = DurableContinuationService(repository)
    provider = RunnerBoundSubagentProvider(first, lambda state, save, cancellation: "persisted")
    service = DelegationService(provider, worker_registry=ContinuationWorkerRegistry(repository))
    context = local_owner_context(correlation_id="delegation-1", workspace_roots=(root,))
    result = service.dispatch(request, context).result(timeout=5)
    assert result.output == "persisted"
    with pytest.raises(IntegrationError, match="commands"):
        service.integrate(request, result, verification=contract.success_criteria, verification_commands=(("pytest",),))


def test_contract_requires_durable_registry_for_dispatch_and_integration(tmp_path):
    preset = resolve_preset("researcher")
    root = tmp_path / "repo"
    workspace = WorkspaceAssignment((str(root),), ())
    contract = WorkerExecutionContract(("tests pass",), (("pytest", "-q"),))
    lineage = LineageRecord("line-1", "root-1", "parent-1", "child-1", 1, preset.name, preset.role, workspace)
    request = DelegationRequest("delegation-1", lineage, "research", preset, workspace, execution_contract=contract)

    class Provider:
        def spawn(self, request, context):
            raise AssertionError("contract dispatch must be gated before provider spawn")

    service = DelegationService(Provider())
    context = local_owner_context(correlation_id="delegation-1", workspace_roots=(root,))
    with pytest.raises(IntegrationError, match="durable worker registry"):
        service.dispatch(request, context)
    result = SubagentResult("child-1", "parent-1", SubagentStatus.SUCCEEDED, output="done", usage=SubagentUsage(steps=1))
    with pytest.raises(IntegrationError, match="durable worker registry"):
        service.integrate(request, result, verification=contract.success_criteria, verification_commands=contract.verification_commands)


@pytest.mark.parametrize(
    "criteria_json,commands_json",
    [
        ('{"criterion": "tests pass"}', '[]'),
        ('[]', '["pytest", "-q"]'),
        ('[]', '[[]]'),
        ('[]', '[["pytest", 7]]'),
    ],
)
def test_malformed_persisted_execution_contract_fails_closed(tmp_path, criteria_json, commands_json):
    repository = SQLiteDurableContinuationRepository(tmp_path / "malformed-contract.sqlite")
    root = SubagentRequest(
        "root-1", "provider root", SubagentBudget(max_steps=8), "root-1", (("provider_root", "true"),),
    )
    repository.create(DurableChildSession(root, ChildSessionLineage("root-1")))
    child = SubagentRequest(
        "root-1", "worker prompt", SubagentBudget(max_steps=8), "child-1",
        (
            ("worker_registry_admitted", "true"),
            ("worker_role", "researcher"),
            ("model", "local-provider"),
            ("backend", "subagent-provider"),
            ("effort", "default"),
            ("scope", str(tmp_path)),
            ("allowed_tools", "research"),
            ("owner_id", "owner"),
            ("worker_id", "child-1"),
            ("retry_max_attempts", "1"),
            ("execution_success_criteria", criteria_json),
            ("execution_verification_commands", commands_json),
        ),
        "resume-1", "idempotency-1",
    )
    repository.create(DurableChildSession(child, ChildSessionLineage("root-1")))
    with pytest.raises(WorkerRegistryError, match="execution contract"):
        ContinuationWorkerRegistry(repository).get("child-1")


def _root_repository(path):
    repository = SQLiteDurableContinuationRepository(path)
    repository.create(DurableChildSession(
        SubagentRequest(
            "root-1", "provider root",
            SubagentBudget(max_steps=30),
            "root-1", (("provider_root", "true"),),
        ),
        ChildSessionLineage("root-1"),
    ))
    return repository


_DIGEST_A = "a" * 64
_DIGEST_B = "b" * 64


@pytest.mark.parametrize(
    "kwargs,match",
    [
        ({"context_policy": "inherit"}, "inherited_context_sha256"),
        ({"context_policy": "scoped"}, "explicit context inputs"),
        ({"context_policy": "clean", "context_inputs": (WorkerContextInput("spec.md", _DIGEST_A),)}, "may not carry"),
        ({"context_inputs": (WorkerContextInput("spec.md", _DIGEST_A),)}, "may not carry"),
        ({"context_policy": "clean", "inherited_context_sha256": _DIGEST_A}, "only inherit"),
        ({"context_policy": "inherit", "inherited_context_sha256": "ABC"}, "sha256"),
        ({"context_policy": "sideways"}, "context_policy"),
        (
            {
                "context_policy": "scoped",
                "context_inputs": (WorkerContextInput("spec.md", _DIGEST_A), WorkerContextInput("spec.md", _DIGEST_B)),
            },
            "conflicting digests",
        ),
        ({"owned_files": ("src/../secrets.txt",)}, r"'\.\.'"),
        ({"owned_files": (os.path.abspath("src/a.py"),), "speculative_lane": True}, "speculative lanes"),
        ({"owned_files": ("src/a.py",)}, "absolute"),
    ],
)
def test_execution_contract_context_and_ownership_validation(kwargs, match):
    with pytest.raises(WorkerRegistryError, match=match):
        WorkerExecutionContract(**kwargs)


def test_execution_contract_normalizes_context_and_ownership(tmp_path):
    contract = WorkerExecutionContract(
        context_policy=WorkerContextPolicy.SCOPED,
        context_inputs=(WorkerContextInput("b.md", _DIGEST_B), WorkerContextInput("a.md", _DIGEST_A)),
        owned_files=(
            str(tmp_path / "src" / "pkg" / "mod.py"),
            str(tmp_path / "src" / "." / "pkg" / "mod.py"),
            str(tmp_path / "docs") + os.sep,
        ),
        task_scope="  issue-510 contract  ",
    )
    assert [item.reference for item in contract.context_inputs] == ["a.md", "b.md"]
    canonical = os.path.normcase(str(tmp_path.resolve())).replace("\\", "/")
    assert contract.owned_files == (canonical + "/docs", canonical + "/src/pkg/mod.py")
    assert contract == WorkerExecutionContract(
        context_policy=WorkerContextPolicy.SCOPED,
        context_inputs=contract.context_inputs,
        owned_files=contract.owned_files,
        task_scope=contract.task_scope,
    )
    assert contract.task_scope == "issue-510 contract"
    assert contract.requested
    assert not WorkerExecutionContract().requested
    assert owned_paths_overlap("/r/pkg", "/r/pkg/mod.py")
    assert not owned_paths_overlap("/r/pkg", "/r/pkg2")


def test_owned_paths_overlap_compares_canonical_strings_exactly():
    # Case folding belongs to canonicalization (os.path.normcase), not to the
    # overlap comparison; distinct canonical strings are distinct paths.
    assert not owned_paths_overlap("/r/A.py", "/r/a.py")
    assert not owned_paths_overlap("/r/Pkg", "/r/pkg/mod.py")


@pytest.mark.skipif(os.name == "nt", reason="POSIX filesystems are case-sensitive; Windows normcase folds case")
def test_posix_case_distinct_owned_files_do_not_conflict(tmp_path):
    upper = WorkerExecutionContract(owned_files=(str(tmp_path / "A.py"),))
    lower = WorkerExecutionContract(owned_files=(str(tmp_path / "a.py"),))
    assert upper.owned_files != lower.owned_files
    assert upper.conflicts_with(lower) == ""


@pytest.mark.skipif(os.name != "nt", reason="Windows-only: normcase folds case on case-insensitive filesystems")
def test_windows_case_variant_owned_files_still_conflict(tmp_path):
    upper = WorkerExecutionContract(owned_files=(str(tmp_path / "A.py").upper(),))
    lower = WorkerExecutionContract(owned_files=(str(tmp_path / "a.py"),))
    assert upper.owned_files == lower.owned_files
    assert "overlaps" in upper.conflicts_with(lower)


def test_full_contract_survives_restart_and_is_recorded_with_terminal_verification(tmp_path):
    database = tmp_path / "full-contract.sqlite"
    root = tmp_path / "repo"
    write_root = root / "write"
    owned = (write_root / "module.py").as_posix()
    preset = resolve_preset("researcher")
    workspace = WorkspaceAssignment((str(root),), (str(write_root),))
    contract = WorkerExecutionContract(
        success_criteria=("tests pass",),
        verification_commands=(("python", "-m", "pytest", "-q"),),
        context_policy="scoped",
        context_inputs=(WorkerContextInput("docs/spec.md", _DIGEST_A),),
        owned_files=(owned,),
        task_scope="issue-510/contract",
    )
    lineage = LineageRecord("line-1", "root-1", "root-1", "child-1", 1, preset.name, preset.role, workspace)
    request = DelegationRequest("delegation-1", lineage, "implement", preset, workspace, execution_contract=contract)
    repository = _root_repository(database)
    provider = RunnerBoundSubagentProvider(
        DurableContinuationService(repository), lambda state, save, cancellation: "done"
    )
    service = DelegationService(provider, worker_registry=ContinuationWorkerRegistry(repository))
    context = local_owner_context(correlation_id="delegation-1", workspace_roots=(root,))
    result = service.dispatch(request, context).result(timeout=5)
    assert result.output == "done"
    reopened_registry = ContinuationWorkerRegistry(SQLiteDurableContinuationRepository(database))
    restored = reopened_registry.get("child-1")
    assert restored is not None and restored.launch.execution_contract == contract
    service.integrate(
        request,
        result,
        verification=contract.success_criteria,
        verification_commands=contract.verification_commands,
    )
    terminal = reopened_registry.get("child-1").terminal_verification
    assert terminal["context_policy"] == "scoped"
    assert terminal["context_inputs"] == (("docs/spec.md", _DIGEST_A),)
    assert terminal["owned_files"] == contract.owned_files
    assert len(contract.owned_files) == 1 and contract.owned_files[0].endswith("/write/module.py")
    assert terminal["task_scope"] == "issue-510/contract"


def test_inherit_contract_mismatch_cannot_certify_result(tmp_path):
    database = tmp_path / "inherit.sqlite"
    root = tmp_path / "repo"
    preset = resolve_preset("researcher")
    workspace = WorkspaceAssignment((str(root),), ())
    contract = WorkerExecutionContract(context_policy="inherit", inherited_context_sha256=_DIGEST_A)
    lineage = LineageRecord("line-1", "root-1", "root-1", "child-1", 1, preset.name, preset.role, workspace)
    request = DelegationRequest("delegation-1", lineage, "review", preset, workspace, execution_contract=contract)
    repository = _root_repository(database)
    provider = RunnerBoundSubagentProvider(
        DurableContinuationService(repository), lambda state, save, cancellation: "done"
    )
    service = DelegationService(provider, worker_registry=ContinuationWorkerRegistry(repository))
    context = local_owner_context(correlation_id="delegation-1", workspace_roots=(root,))
    result = service.dispatch(request, context).result(timeout=5)
    assert result.output == "done"
    drifted = replace(
        request,
        execution_contract=WorkerExecutionContract(context_policy="inherit", inherited_context_sha256=_DIGEST_B),
    )
    with pytest.raises(IntegrationError, match="does not match request"):
        service.integrate(drifted, result)
    service.integrate(request, result)


@pytest.mark.parametrize("where", ["elsewhere", "repo"])
def test_owned_files_must_be_inside_write_assignment(tmp_path, where):
    root = tmp_path / "repo"
    # "repo" is readable but not inside the "repo/write" write root.
    owned = str(tmp_path / where / "module.py")
    preset = resolve_preset("researcher")
    workspace = WorkspaceAssignment((str(root),), (str(root / "write"),))
    contract = WorkerExecutionContract(owned_files=(owned,))
    lineage = LineageRecord("line-1", "root-1", "root-1", "child-1", 1, preset.name, preset.role, workspace)
    request = DelegationRequest("delegation-1", lineage, "implement", preset, workspace, execution_contract=contract)

    class Registry:
        def admit(self, launch):
            raise AssertionError("ownership must be validated before admission")

    class Provider:
        def spawn(self, request, context):
            raise AssertionError("ownership must be validated before spawn")

    service = DelegationService(Provider(), worker_registry=Registry())
    with pytest.raises(IntegrationError, match="owned file"):
        service.dispatch(request, local_owner_context(correlation_id="d", workspace_roots=(root,)))


def _owned_launch(root, worker_id, key, contract):
    return replace(
        _launch(root, worker_id=worker_id),
        parent_id="root-1", resume_key=key, idempotency_key=key, execution_contract=contract,
    )


def test_active_owned_file_overlap_rejects_second_worker_until_first_is_terminal(tmp_path):
    repository = _root_repository(tmp_path / "owned.sqlite")
    registry = ContinuationWorkerRegistry(repository)
    first = _owned_launch(tmp_path / "repo", "child-1", "task-1", WorkerExecutionContract(owned_files=(str(tmp_path / "repo" / "src" / "pkg"),)))
    registry.admit(first)
    overlapping = _owned_launch(
        tmp_path / "repo", "child-2", "task-2", WorkerExecutionContract(owned_files=(str(tmp_path / "repo" / "src" / "pkg" / "mod.py"),))
    )
    with pytest.raises(DuplicateWorkerError, match="overlaps"):
        registry.admit(overlapping)
    assert repository.get("child-2") is None
    disjoint = _owned_launch(
        tmp_path / "repo", "child-3", "task-3", WorkerExecutionContract(owned_files=(str(tmp_path / "repo" / "src" / "pkg2" / "mod.py"),))
    )
    assert registry.admit(disjoint).status is WorkerStatus.QUEUED

    service = DurableContinuationService(repository)
    handle = service.spawn(
        repository.get("child-1").request,
        local_owner_context(correlation_id="task-1", workspace_roots=(tmp_path / "repo",)),
        lambda state, save, cancellation: "released",
    )
    assert handle.result(timeout=5).output == "released"
    assert registry.admit(overlapping).status is WorkerStatus.QUEUED


def test_duplicate_task_scope_requires_both_lanes_to_be_speculative(tmp_path):
    repository = _root_repository(tmp_path / "task.sqlite")
    registry = ContinuationWorkerRegistry(repository)
    registry.admit(_owned_launch(
        tmp_path / "repo", "child-1", "task-1",
        WorkerExecutionContract(task_scope="issue-510", speculative_lane=True),
    ))
    with pytest.raises(DuplicateWorkerError, match="task scope"):
        registry.admit(_owned_launch(
            tmp_path / "repo", "child-2", "task-2", WorkerExecutionContract(task_scope="issue-510"),
        ))
    speculative = registry.admit(_owned_launch(
        tmp_path / "repo", "child-3", "task-3",
        WorkerExecutionContract(task_scope="issue-510", speculative_lane=True),
    ))
    assert speculative.launch.execution_contract.speculative_lane is True


def test_legacy_two_key_contract_rows_still_restore(tmp_path):
    repository = _root_repository(tmp_path / "legacy.sqlite")
    child = SubagentRequest(
        "root-1", "worker prompt", SubagentBudget(max_steps=8), "child-1",
        (
            ("worker_registry_admitted", "true"),
            ("worker_role", "researcher"),
            ("owner_id", "owner"),
            ("worker_id", "child-1"),
            ("execution_success_criteria", '["tests pass"]'),
            ("execution_verification_commands", '[["pytest","-q"]]'),
        ),
        "resume-1", "idempotency-1",
    )
    repository.create(DurableChildSession(child, ChildSessionLineage("root-1")))
    record = ContinuationWorkerRegistry(repository).get("child-1")
    assert record.launch.execution_contract == WorkerExecutionContract(("tests pass",), (("pytest", "-q"),))
    assert record.launch.execution_contract.context_policy is WorkerContextPolicy.UNSPECIFIED


@pytest.mark.parametrize(
    "key,value",
    [
        ("execution_context_policy", "sideways"),
        ("execution_context_inputs", '[["spec.md"]]'),
        ("execution_context_inputs", '[["spec.md","not-a-digest"]]'),
        ("execution_owned_files", '"src/a.py"'),
        ("execution_owned_files", '["../escape"]'),
        ("execution_speculative_lane", "yes"),
        ("execution_inherited_context_sha256", "a" * 64),
    ],
)
def test_malformed_persisted_context_contract_fails_closed(tmp_path, key, value):
    repository = _root_repository(tmp_path / "malformed-context.sqlite")
    child = SubagentRequest(
        "root-1", "worker prompt", SubagentBudget(max_steps=8), "child-1",
        (("worker_registry_admitted", "true"), ("owner_id", "owner"), ("worker_id", "child-1"), (key, value)),
        "resume-1", "idempotency-1",
    )
    if key in {"execution_owned_files", "execution_speculative_lane"}:
        # Ownership now joins the canonical admission transaction, so an
        # invalid owned-file/scope claim fails before a worker is reserved.
        with pytest.raises(InvalidSubagentRequest, match="ownership contract"):
            repository.create(DurableChildSession(child, ChildSessionLineage("root-1")))
        return
    repository.create(DurableChildSession(child, ChildSessionLineage("root-1")))
    with pytest.raises(WorkerRegistryError, match="execution contract"):
        ContinuationWorkerRegistry(repository).get("child-1")


def test_failed_worker_with_contract_records_failure_instead_of_raising(tmp_path):
    database = tmp_path / "failed-contract.sqlite"
    root = tmp_path / "repo"
    preset = resolve_preset("researcher")
    workspace = WorkspaceAssignment((str(root),), ())
    contract = WorkerExecutionContract(("tests pass",), (("pytest", "-q"),))
    lineage = LineageRecord("line-1", "root-1", "root-1", "child-1", 1, preset.name, preset.role, workspace)
    request = DelegationRequest("delegation-1", lineage, "implement", preset, workspace, execution_contract=contract)
    repository = _root_repository(database)

    def failing_runner(state, save, cancellation):
        raise RuntimeError("worker crashed")

    provider = RunnerBoundSubagentProvider(DurableContinuationService(repository), failing_runner)
    service = DelegationService(provider, worker_registry=ContinuationWorkerRegistry(repository))
    context = local_owner_context(correlation_id="delegation-1", workspace_roots=(root,))
    handle = service.dispatch(request, context)
    failed = handle.result(timeout=5)
    assert failed.status is SubagentStatus.FAILED
    assert failed.error is not None and failed.error.message == "worker crashed"
    drifted = replace(request, execution_contract=WorkerExecutionContract(("other",)))
    with pytest.raises(IntegrationError, match="does not match request"):
        service.integrate(drifted, failed)
    delegated = service.integrate(request, failed)
    assert delegated.evidence.status.value == "failed"
    terminal = ContinuationWorkerRegistry(SQLiteDurableContinuationRepository(database)).get("child-1").terminal_verification
    assert terminal["status"] == "failed"
    assert terminal["output_digest"] == delegated.evidence.output_digest
    assert terminal["success_criteria"] == ("tests pass",)


def test_registry_contract_rejects_relative_owned_files_bypass(tmp_path):
    with pytest.raises(WorkerRegistryError, match="absolute"):
        WorkerExecutionContract(owned_files=("src/a.py",))
    repository = _root_repository(tmp_path / "bypass.sqlite")
    registry = ContinuationWorkerRegistry(repository)
    absolute = tmp_path / "repo" / "src" / "a.py"
    registry.admit(_owned_launch(tmp_path / "repo", "child-1", "task-1", WorkerExecutionContract(owned_files=(str(absolute),))))
    respelled = str(tmp_path / "repo" / "src" / "." / "a.py")
    if os.name == "nt":
        respelled = respelled.upper()
    with pytest.raises(DuplicateWorkerError, match="overlaps"):
        registry.admit(_owned_launch(tmp_path / "repo", "child-2", "task-2", WorkerExecutionContract(owned_files=(respelled,))))


def test_owned_files_resolve_symlinked_spellings(tmp_path):
    real = tmp_path / "real"
    (real / "src").mkdir(parents=True)
    link = tmp_path / "link"
    try:
        os.symlink(real, link, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink unavailable: {exc}")
    via_link = WorkerExecutionContract(owned_files=(str(link / "src" / "a.py"),))
    via_real = WorkerExecutionContract(owned_files=(str(real / "src" / "a.py"),))
    assert via_link.owned_files == via_real.owned_files
    assert via_link.conflicts_with(via_real)
