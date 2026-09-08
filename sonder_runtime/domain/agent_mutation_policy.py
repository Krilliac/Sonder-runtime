"""Pure policy for deciding whether an agent tool invocation mutates state."""

from __future__ import annotations


WORK_MUTATION_TOOLS = frozenset({
    "directory_create", "file_write", "file_batch_write", "json_patch", "file_edit", "file_copy", "file_move", "file_delete", "text_patch", "data_convert",
    "sqlite_mutate", "scaffold_project", "archive_extract", "archive_create",
    "fetch_artifact", "artifact_generate", "game_generate_and_test", "game_generation_campaign",
    "ensemble_codegen_build_loop", "memory_quality_repair", "memory_privacy_repair",
    "memory_embedding_backfill", "memory_interaction_embedding_backfill",
    "git_commit", "git_branch", "git_checkout", "git_stash", "git_tag", "git_merge", "git_cherry_pick",
    "dependency_add", "dependency_remove", "dependency_update", "build_clean", "rename_symbol", "apply_patch",
    "lint_run", "format_code", "task_delete",
})


def invocation_mutates(tool_name, args):
    """Return whether this invocation can change persistent workspace state."""
    args = args if isinstance(args, dict) else {}
    if tool_name == "agent_lane":
        return args.get("action") in {"spawn", "send_message", "message", "resume"}
    if tool_name not in WORK_MUTATION_TOOLS:
        return False
    if tool_name == "file_delete":
        return args.get("dry_run") is False
    if tool_name == "json_patch":
        return str(args.get("mode", "preview")).strip().lower() == "apply"
    if tool_name == "text_patch":
        return args.get("apply") is True
    if tool_name == "rename_symbol":
        return args.get("dry_run") is False
    if tool_name == "apply_patch":
        return args.get("check_only") is not True
    if tool_name == "lint_run":
        return args.get("fix") is True
    if tool_name == "format_code":
        return args.get("check_only") is not True
    if tool_name == "data_convert":
        return args.get("apply") is True
    if tool_name == "sqlite_mutate":
        return str(args.get("mode", "preview")).strip().lower() == "apply"
    if tool_name in {
        "memory_quality_repair", "memory_privacy_repair",
        "memory_embedding_backfill", "memory_interaction_embedding_backfill",
    }:
        return args.get("apply") is True
    return True
