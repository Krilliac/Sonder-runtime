"""Bind delegated workers to the existing guarded repository agent loop.

The coordinator supplies creation folders, never the model. Tool availability
does not authorize effects: the agent dispatcher still applies permission modes,
approvals, inspection requirements and project path/execution checks.
"""
from __future__ import annotations

from sonder_runtime.adapters.fleet_creations import is_provisioned_worker


BUILD_TOOLS = frozenset({
    "workspace_inventory", "directory_tree", "file_find", "file_read",
    "file_read_range", "text_search", "script_search", "file_write",
    "file_batch_write", "file_edit", "text_patch", "directory_create",
    "workspace_run", "script_run", "run_code", "test_run", "file_check",
})


def repository_worker(
    tier, project, max_steps, *, build, orchestrator, activity, agent_impl,
    project_tools, unsafe_active,
):
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

    def worker(prompt, assigned_project):
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
            options = {}
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
