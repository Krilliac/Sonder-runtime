"""Pure domain policy for safe speculative tool execution."""

from __future__ import annotations

from .tools.builtin_traits import builtin_traits
from .tools.traits import ToolTraits


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


def is_speculatable(tool_name: str, traits: ToolTraits | None = None) -> bool:
    """Return whether a tool is safe to issue before branch resolution.

    The closed host allowlist remains the legacy gate.  When metadata is
    supplied, it is an additional gate: only a host-declared read-only trait
    can authorize speculation.  Advisory metadata from an external server
    therefore cannot grant this optimization.
    """
    if tool_name not in SPECULATABLE_TOOLS:
        return False
    if traits is None:
        traits = builtin_traits(tool_name)
    return isinstance(traits, ToolTraits) and traits.is_read_only


__all__ = ["SPECULATABLE_TOOLS", "is_speculatable"]
