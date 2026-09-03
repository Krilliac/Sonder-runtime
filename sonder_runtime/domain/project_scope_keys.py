"""Project-scoped path key resolution for tool arguments.
"""
from __future__ import annotations


def project_scoped_path_key(tool_name):
    if tool_name == "ensemble_codegen_build_loop":
        return "project_dir"
    if tool_name == "archive_extract":
        return "destination"
    if tool_name == "fetch_artifact":
        # Its download target is `dest`; there is no `path` parameter, so the
        # generic branch was reading a missing key, falling back to ".", and
        # passing unconditionally. Latent rather than live today (this tool is
        # neither advertised nor dispatchable), but it arms the moment anyone
        # gives it a dispatch branch -- which is exactly what this task just
        # did for twenty-three of its neighbours.
        return "dest"
    if tool_name in {
        "file_find", "text_search", "script_search", "scaffold_project",
        "repo_status", "repo_diff", "text_patch", "archive_create",
        # Developer-workflow tools (harness_tools.py) all take "root", not
        # "path" -- without this, a project-bound run would silently
        # rebase a nonexistent "path" key while the real "root" argument
        # (and its escape-check) went untouched.
        "test_discover", "test_run", "lint_run", "format_code", "typecheck_run",
        "dependency_add", "dependency_remove", "dependency_update", "dependency_audit",
        "git_commit", "git_branch", "git_checkout", "git_stash", "git_tag",
        "git_merge", "git_cherry_pick",
        "build_run", "build_clean",
        "rename_symbol", "find_references", "diff_files", "apply_patch", "secret_scan",
    }:
        return "root"
    return "path"
