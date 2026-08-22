"""Pure domain policy for safe speculative tool execution."""

from __future__ import annotations


# A speculative call must be read-only, local, and unable to spend a cloud
# budget. Keep this allowlist closed: new tools are non-speculatable until
# their side-effect contract is reviewed.
SPECULATABLE_TOOLS = frozenset({
    "workspace_inventory",
    "directory_tree",
    "file_find",
    "file_read",
    "file_read_range",
    "text_search",
    "script_search",
    "program_search",
    "image_inspect",
    "data_inspect",
    "memory_search",
    "activity_status",
    "context_health",
    "status",
    "command_registry_list",
    "permission_policy",
})


def is_speculatable(tool_name: str) -> bool:
    """Return whether a tool is safe to issue before branch resolution."""
    return tool_name in SPECULATABLE_TOOLS


__all__ = ["SPECULATABLE_TOOLS", "is_speculatable"]
