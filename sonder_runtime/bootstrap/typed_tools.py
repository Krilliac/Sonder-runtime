"""The tool family the typed gateway admits, and the policy that admits it.

Slice one of the typed-tool migration is the read-only workbench family:
``directory_tree``, ``file_find``, ``read_file`` (the legacy ``file_read``),
``file_read_range``, ``text_search``, ``script_search`` and
``program_search``.  All seven are ``safe``-class reads on the permission
matrix, already contained by the shared adapter guards, and present on both
the legacy and native catalogs.  Everything else is denied by the resource
policy composed here until it is migrated deliberately.

The descriptors are the native catalog's, with the two guard knobs the legacy
handlers pass (``bypass`` and ``developer_authorized``) admitted by schema.
The native MCP surface validates against its own schema first, which omits
both, so a native client still cannot ask for either; only the in-process
legacy forwards, which derive them from a token or approval, can.
"""
from __future__ import annotations

import copy

from ..application.ports.tool_registry import InMemoryToolRegistry, ToolDescriptor
from ..application.tools.resource_policy import Decision, PolicyRule, ResourcePolicy

READ_ONLY_TOOLS = (
    "directory_tree", "file_find", "file_read_range", "program_search",
    "read_file", "script_search", "text_search",
)

# Canonical (typed) name -> the name the permission catalog grades.
POLICY_NAMES = {"read_file": "file_read"}

# Legacy handler name -> canonical typed name.
LEGACY_TO_CANONICAL = {"file_read": "read_file"}

# Which guard knobs each tool's own primitive accepts; a knob a primitive does
# not take must not be sent, or the call fails on the primitive's signature.
GUARD_KNOBS = {
    "directory_tree": ("extra_roots", "bypass"),
    "file_find": ("extra_roots", "bypass"),
    "file_read_range": ("extra_roots", "bypass", "developer_authorized"),
    "program_search": (),
    "read_file": ("extra_roots", "bypass", "developer_authorized"),
    "script_search": ("extra_roots", "bypass"),
    "text_search": ("extra_roots", "bypass"),
}

_READ_FILE_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "minLength": 1},
        "max_bytes": {"type": "integer"},
        "extra_roots": {"type": "string"},
    },
    "required": ["path"],
    "additionalProperties": False,
}


def typed_tool_registry() -> InMemoryToolRegistry:
    """Descriptors for the admitted family, derived from the native catalog."""
    from .native_mcp import native_tool_registry

    native = native_tool_registry()
    descriptors = []
    for name in READ_ONLY_TOOLS:
        base = native.get(name)
        if base is None:
            raise LookupError("native catalog no longer carries %r" % name)
        schema = copy.deepcopy(_READ_FILE_SCHEMA if name == "read_file" else base.input_schema)
        properties = schema.setdefault("properties", {})
        for knob in GUARD_KNOBS[name]:
            properties.setdefault(knob, {"type": "boolean"} if knob != "extra_roots"
                                  else {"type": "string"})
        descriptors.append(ToolDescriptor(
            name, base.description, schema,
            effects=base.effects, execution_class=base.execution_class,
        ))
    return InMemoryToolRegistry(descriptors)


def read_only_policy() -> ResourcePolicy:
    """Allow exactly the admitted family; the default for anything else is deny."""
    return ResourcePolicy(
        PolicyRule(
            "read-only:%s" % name, Decision.ALLOW, tool=name,
            reason="read-only workbench family; the adapter guards contain it",
        )
        for name in READ_ONLY_TOOLS
    )


__all__ = [
    "GUARD_KNOBS", "LEGACY_TO_CANONICAL", "POLICY_NAMES", "READ_ONLY_TOOLS",
    "read_only_policy", "typed_tool_registry",
]
