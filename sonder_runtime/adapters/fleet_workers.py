"""Bind delegated workers to the existing guarded repository agent loop.

The coordinator supplies creation folders, never the model. Tool availability
does not authorize effects: the agent dispatcher still applies permission modes,
approvals, inspection requirements and project path/execution checks.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from sonder_runtime.adapters.fleet_creations import is_provisioned_worker

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from contextlib import AbstractContextManager
    from typing import Protocol, TypedDict, TypeVar

    _ResultT_co = TypeVar("_ResultT_co", covariant=True)
    _ResultT = TypeVar("_ResultT")

    class _ResponseActivity(Protocol):
        def current_response_id(self) -> str | None: ...

        def bind_response(self, response_id: str | None) -> AbstractContextManager[object]: ...

    class _RepositoryOrchestrator(Protocol[_ResultT_co]):
        def resolve_repository_project_root(self, task: str, project: str = ...) -> str: ...

        def same_project_root(self, left: str, right: str) -> bool: ...

        @property
        def current_worker_cancel_requested(self) -> Callable[[], bool]: ...

        def repository_worker_result(self, receipt: object, expected_project: str) -> _ResultT_co: ...

    class _BuildOptions(TypedDict, total=False):
        tool_allowlist: frozenset[str]
        require_guarded_project: bool

    class _AgentImpl(Protocol):
        def __call__(
            self,
            prompt: str,
            *,
            tier: str,
            max_steps: int,
            allow_web: bool,
            require_file_evidence: bool,
            read_only: bool,
            include_evidence: bool,
            auto_checklist: bool,
            project: str,
            return_host_receipt: bool,
            cancel_check: Callable[..., bool],
            tool_allowlist: frozenset[str] = ...,
            require_guarded_project: bool = ...,
        ) -> object: ...


BUILD_TOOLS = frozenset({
    "workspace_inventory", "directory_tree", "file_find", "file_read",
    "file_read_range", "text_search", "script_search", "file_write",
    "file_batch_write", "file_edit", "text_patch", "directory_create",
    "workspace_run", "script_run", "run_code", "test_run", "file_check",
})


def repository_worker(
    tier: str,
    project: str,
    max_steps: int,
    *,
    build: bool,
    orchestrator: _RepositoryOrchestrator[_ResultT],
    activity: _ResponseActivity,
    agent_impl: _AgentImpl,
    project_tools: Iterable[str],
    unsafe_active: Callable[[], bool],
) -> Callable[[str, str], _ResultT]:
    response_id = activity.current_response_id()
    project_scope = (
        orchestrator.resolve_repository_project_root("", project)
        if project or not build else ""
    )

    # A greenfield lane has no caller-supplied repository.  Its project must
    # therefore be one of the exact worker roots issued by fleet_creations;
    # accepting a resolver-selected path here would let a model/task redirect
    # the lane to a sibling or parent directory before the agent loop starts.
    greenfield = bool(build and not project)

    def worker(prompt: str, assigned_project: str) -> _ResultT:
        with activity.bind_response(response_id):
            if greenfield and not is_provisioned_worker(assigned_project):
                raise RuntimeError("greenfield worker assignment is not host-provisioned")
            assigned_anchor = (
                orchestrator.resolve_repository_project_root("", assigned_project)
                if greenfield else assigned_project
            )
            assigned = orchestrator.resolve_repository_project_root(prompt, assigned_project)
            expected = project_scope or assigned_anchor
            if greenfield and not orchestrator.same_project_root(assigned, assigned_anchor):
                raise RuntimeError("greenfield worker assignment changed after fleet start")
            if not orchestrator.same_project_root(assigned, expected):
                raise RuntimeError("repository worker assignment changed after fleet start")
            options: _BuildOptions = {}
            if build:
                # Unsafe-lab deliberately removes the ordinary agent gates. A
                # creation run promises those gates, so fail closed in that mode.
                if unsafe_active():
                    raise RuntimeError("build fleet requires normal project and permission gates")
                # Only advertise tools with an existing project-bound contract.
                # In particular, unscoped run_code must not gain one by accident.
                options["tool_allowlist"] = BUILD_TOOLS.intersection(project_tools)
                options["require_guarded_project"] = True
            receipt = agent_impl(
                prompt, tier=tier, max_steps=max_steps, allow_web=False,
                require_file_evidence=True, read_only=not build,
                include_evidence=True, auto_checklist=True, project=expected,
                return_host_receipt=True,
                cancel_check=orchestrator.current_worker_cancel_requested,
                **options,
            )
            return orchestrator.repository_worker_result(receipt, expected)

    return worker
