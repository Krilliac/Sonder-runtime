"""Host declarations for the built-in tools, including legacy spelling aliases.

Read declarations preserve the repository read-only policy and the catalog's
verified reads. The remaining traits are intentionally narrower: read-only
does not prove thread safety or absence of external access. New/unclassified
tools get UNKNOWN, never an optimistic inference from their name.
"""
from __future__ import annotations

from .traits import ToolTraits, TriState, traits_from_effects


# Existing host-owned read contracts in server.REPOSITORY_READ_ONLY_TOOLS,
# command_catalog._READ_ONLY, typed_tools and permission_modes native grades.
READ_ONLY_TOOLS = frozenset({
    "tool_help", "file_check",
    "file_policy", "workspace_inventory", "workspace_compare", "directory_tree",
    "file_find", "dependency_inventory", "repository_symbol_index", "log_inspect",
    "file_read", "file_digest", "directory_digest", "file_read_range", "context_pack",
    "repo_status", "repo_diff", "repo_log", "repo_show", "repo_blame", "project_detect",
    "data_inspect", "data_query", "archive_list", "artifact_risk_inspect", "verify_artifact",
    "text_search", "script_search", "program_search", "image_inspect", "command_registry_list",
    "activity_status", "permission_policy", "context_compaction_plan", "diagnostics",
    "context_health", "learning_health_status", "context_policy_status", "artifact_ground",
    "evaluation_history_status", "memory_quality_report", "memory_privacy_review",
    "system_improvement_report", "master_status", "master_capacity", "self_heal_check",
    "status", "system_profile_text", "environment_status", "toolchain_status",
    "hardware_profile", "tool_inventory", "output_digest", "emotion_vector_status",
    "preferences_status", "tool_manifest", "memory_search", "web_search", "web_fetch",
    "weather_lookup", "test_discover", "computer_use_status", "task_list", "task_show",
    "checklist_show", "admin_status", "admin_whoami", "autopilot_status", "calibration_status",
    "learn_tiers", "live_reload_status", "mcp_runtime_status", "reasoning_show", "sonder_sessions",
    "sonder_stats", "turn_inspect", "workflow_list", "memory_export", "policy_explain",
    "runtime_policy_status", "runtime_source_update_status", "runtime_source_stash_status",
    "permission_approvals", "test_run_result", "build_model", "build_job_result",
    "build_fix_result", "crash_triage", "profile_digest", "debug_run_result",
    "process_list", "process_memory_risk_inspect", "approximate_location_lookup",
    "compute_status", "compute_artifact_fetch", "tool_search",
})

_ALIASES = {
    "read_file": "file_read", "write_file": "file_write", "edit_file": "file_edit",
    "make_directory": "directory_create", "run_program": "workspace_run",
    "run_script": "script_run",
}

# Independent bounded filesystem reads have no shared mutable invocation state.
# Cache refreshers, host probes, model-backed inspections and job handles are
# deliberately absent, even when their existing read-only contract is retained.
_PARALLEL_READS = frozenset({
    "directory_tree", "file_find", "file_read", "file_read_range", "file_digest",
    "text_search", "image_inspect", "output_digest",
})
_LOCAL_READS = _PARALLEL_READS | frozenset({
    "workspace_inventory", "environment_status", "toolchain_status", "tool_inventory",
    "hardware_profile", "file_policy", "repo_status", "repo_diff", "artifact_risk_inspect",
    "process_list", "process_memory_risk_inspect",
})
_NETWORK = frozenset({"web_search", "web_fetch", "weather_lookup", "approximate_location_lookup"})
_DESTRUCTIVE = frozenset({
    "file_delete", "sqlite_mutate", "task_delete", "git_merge", "git_cherry_pick",
})
# Scoped writes can create or overwrite depending on arguments; declaring the
# entire tool unconditionally destructive would upgrade ordinary catalogued
# mutations to dangerous. Keep this additional knowledge UNKNOWN and preserve
# the existing permission grade (including build_fix_restore's mutation grade).
_LOCAL_FILE_MUTATIONS = frozenset({
    "file_write", "file_edit", "file_batch_write", "file_copy", "file_move", "file_delete",
    "json_patch", "text_patch",
})


def builtin_traits(name: str, effects=()) -> ToolTraits:
    """Return host metadata; absence is an explicit conservative declaration."""
    name = _ALIASES.get(name, name)
    if name in READ_ONLY_TOOLS:
        return ToolTraits(
            read_only=TriState.TRUE, destructive=TriState.FALSE, idempotent=TriState.TRUE,
            concurrency_safe=TriState.TRUE if name in _PARALLEL_READS else TriState.UNKNOWN,
            open_world=(TriState.TRUE if name in _NETWORK else
                        TriState.FALSE if name in _LOCAL_READS else TriState.UNKNOWN),
        )
    if name in _DESTRUCTIVE:
        return ToolTraits(read_only=TriState.FALSE, destructive=TriState.TRUE,
                          concurrency_safe=TriState.FALSE,
                          open_world=TriState.FALSE if name in _LOCAL_FILE_MUTATIONS else TriState.UNKNOWN)
    if name in _LOCAL_FILE_MUTATIONS or name == "build_fix_restore":
        return ToolTraits(read_only=TriState.FALSE, concurrency_safe=TriState.FALSE,
                          open_world=TriState.FALSE)
    if name == "directory_create":
        return ToolTraits(read_only=TriState.FALSE, destructive=TriState.FALSE,
                          open_world=TriState.FALSE)
    return traits_from_effects(effects)
