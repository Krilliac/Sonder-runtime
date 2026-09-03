"""The tool family the typed gateway admits, and the policy that admits it.

Slice one of the typed-tool migration was the read-only workbench family:
``directory_tree``, ``file_find``, ``read_file`` (the legacy ``file_read``),
``file_read_range``, ``text_search``, ``script_search`` and
``program_search`` -- all ``safe``-class reads on the permission matrix.
Slice two adds the mutating file family: ``write_file`` (legacy
``file_write``), ``edit_file`` (``file_edit``), ``make_directory``
(``directory_create``), ``file_copy``, ``file_move``, ``file_batch_write``,
``json_patch``, ``text_patch`` and ``file_delete``. All of them are contained
by the shared adapter guards and present on both the legacy and native
catalogs; everything else is denied by the resource policy composed here
until it is migrated deliberately.

The descriptors are the native catalog's, with the guard knobs the legacy
handlers pass (``bypass``, ``developer_authorized``) admitted by schema. The
native MCP surface validates against its own schema first, which omits both,
so a native client still cannot ask for either; only the in-process legacy
forwards, which derive them from a token or approval, can.
"""
from __future__ import annotations

import copy

from ..application.ports.tool_registry import InMemoryToolRegistry, ToolDescriptor
from ..application.tools.resource_policy import Decision, PolicyRule, ResourcePolicy

READ_ONLY_TOOLS = (
    "directory_tree", "file_find", "file_read_range", "program_search",
    "read_file", "script_search", "text_search",
)

MUTATING_TOOLS = (
    "edit_file", "file_batch_write", "file_copy", "file_delete", "file_move",
    "json_patch", "make_directory", "text_patch", "write_file",
)

TYPED_TOOLS = READ_ONLY_TOOLS + MUTATING_TOOLS

# Canonical (typed) name -> the name the permission catalog grades.
POLICY_NAMES = {
    "read_file": "file_read",
    "write_file": "file_write",
    "edit_file": "file_edit",
    "make_directory": "directory_create",
}

# Legacy handler name -> canonical typed name.
LEGACY_TO_CANONICAL = {legacy: canonical for canonical, legacy in POLICY_NAMES.items()}

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
    "edit_file": ("extra_roots", "bypass", "developer_authorized"),
    "file_batch_write": ("extra_roots", "bypass"),
    "file_copy": ("extra_roots", "bypass", "developer_authorized"),
    "file_delete": ("extra_roots", "bypass", "developer_authorized"),
    "file_move": ("extra_roots", "bypass", "developer_authorized"),
    "json_patch": ("extra_roots", "bypass"),
    "make_directory": ("extra_roots", "bypass", "developer_authorized"),
    "text_patch": ("extra_roots", "developer_authorized"),
    "write_file": ("extra_roots", "bypass", "developer_authorized"),
}


def typed_tool_registry() -> InMemoryToolRegistry:
    """Descriptors for the admitted family, derived from the native catalog."""
    from .native_mcp import native_tool_registry

    native = native_tool_registry()
    descriptors = []
    for name in TYPED_TOOLS:
        base = native.get(name)
        if base is None:
            raise LookupError("native catalog no longer carries %r" % name)
        schema = copy.deepcopy(base.input_schema)
        if not schema.get("properties"):
            raise LookupError("native descriptor %r has no bounded schema" % name)
        properties = schema["properties"]
        for knob in GUARD_KNOBS[name]:
            properties.setdefault(knob, {"type": "boolean"} if knob != "extra_roots"
                                  else {"type": "string"})
        descriptors.append(ToolDescriptor(
            name, base.description, schema,
            effects=base.effects, execution_class=base.execution_class,
        ))
    return InMemoryToolRegistry(descriptors)


def typed_tool_policy() -> ResourcePolicy:
    """Allow exactly the admitted family; the default for anything else is deny."""
    rules = [
        PolicyRule(
            "read-only:%s" % name, Decision.ALLOW, tool=name,
            reason="read-only workbench family; the adapter guards contain it",
        )
        for name in READ_ONLY_TOOLS
    ]
    rules.extend(
        PolicyRule(
            "mutating:%s" % name, Decision.ALLOW, tool=name,
            reason="mutating file family; the adapter guards contain it and the "
                   "permission gate decides it",
        )
        for name in MUTATING_TOOLS
    )
    return ResourcePolicy(rules)


__all__ = [
    "GUARD_KNOBS", "LEGACY_TO_CANONICAL", "MUTATING_TOOLS", "POLICY_NAMES",
    "READ_ONLY_TOOLS", "TYPED_TOOLS", "typed_tool_policy", "typed_tool_registry",
]
