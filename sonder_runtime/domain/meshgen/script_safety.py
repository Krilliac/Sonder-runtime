"""AST gate for model-written Blender build scripts.

What this enforces (and nothing more is claimed):

* the script parses, defines a top-level ``def build``, and is at most 60 000 characters;
* imports come only from ``bpy``, ``bmesh``, ``mathutils``, ``math``, ``random`` and the
  optional kit module; ``bpy`` may only be imported as plain ``import bpy``;
* ``bpy`` is reachable ONLY through three spellings, each followed directly by an
  allow-listed member: ``bpy.ops.<category>.<operator>(...)`` (categories ``mesh``,
  ``object``, ``transform``, ``curve``), ``bpy.data.<collection>`` (``objects``, ``meshes``,
  ``materials``, ``collections``, ``curves``) and ``bpy.context.<member>`` (scene, view layer,
  active/selected objects, depsgraph).  ``bpy``, ``bpy.ops``, ``bpy.data`` and
  ``bpy.context`` can never be bound to a name, stored in a container, passed, returned or
  used as a default, so there is no alias to launder a denied path through;
* a fixed list of dangerous attribute names (text blocks, libraries, file paths, save/write,
  drivers, preferences, window manager, os/sys/subprocess/...) is refused wherever it
  appears as an attribute or keyword argument, whatever object it hangs off, and string
  literals equal to the data-block names are refused too;
* the kit module is reachable only through its public top-level defs/constants;
* ``str.format`` / ``format_map`` (they resolve attribute chains from string text) and any
  string literal containing ``__`` are refused;
* dynamic-code builtins, dunder/underscore names and ``getattr`` style reflection are refused.

This is a screen against honest mistakes and obvious escapes, defence in depth next to the
harness' restricted builtins, the harness' neutering of ``Text.as_module`` and
``bpy.utils.execfile``, and Blender's ``--factory-startup --disable-autoexec``.  It is NOT a
sandbox: an allowed operator or a Blender bug can still do harm, and the tool has the same
trust level as ``run_code``.  Pure; stdlib only.
"""
from __future__ import annotations

import ast

MAX_SOURCE_CHARS = 60_000

ALLOWED_IMPORT_ROOTS = frozenset({"bpy", "bmesh", "mathutils", "math", "random"})

FORBIDDEN_NAMES = frozenset({
    "open", "exec", "eval", "compile", "__import__", "globals", "locals", "vars",
    "getattr", "setattr", "delattr", "input", "breakpoint", "memoryview", "help",
    "exit", "quit", "type", "dir", "format",
})

# bpy.ops categories: geometry creation/editing only.  Everything else (wm, script, text,
# file, export_scene, import_scene, render, image, material, scene, ...) is refused.
ALLOWED_OPS_CATEGORIES = frozenset({"mesh", "object", "transform", "curve"})
# Operator names containing one of these are refused even inside an allowed category
# (e.g. object.bake_image, object.load_reference_image).
FORBIDDEN_OPERATOR_FRAGMENTS = (
    "import", "export", "save", "load", "open", "write", "script", "python", "bake", "browse", "file",
)
ALLOWED_DATA_COLLECTIONS = frozenset({"objects", "meshes", "materials", "collections", "curves"})
ALLOWED_CONTEXT_MEMBERS = frozenset({
    "scene", "view_layer", "active_object", "object", "selected_objects",
    "selected_editable_objects", "collection", "evaluated_depsgraph_get", "temp_override",
})

# Refused as an attribute name (``x.NAME``) or keyword argument name regardless of what ``x``
# is.  Errs on the side of denying: a legitimate build script only needs
# mesh/object/transform/material/bmesh/mathutils work.  ``write`` is refused outright (also on
# non-bpy objects), since a text block or file handle is the usual way to use it.
FORBIDDEN_ATTRIBUTES = frozenset({
    # text blocks / script execution
    "as_module", "blend_data", "texts", "text", "run_script", "script", "scripts", "execfile",
    "exec_module", "register_class", "unregister_class",
    # libraries and files
    "libraries", "library", "override_library", "filepath", "filepath_raw", "filename", "directory",
    "filepath_from_user", "save", "save_as", "save_render", "save_as_mainfile", "save_mainfile",
    "write", "writelines", "read", "load", "unpack", "pack", "reload", "file", "wm",
    "export_scene", "import_scene", "render",
    # drivers and animation scripting
    "driver_add", "driver_remove", "drivers", "driver", "expression", "animation_data",
    "animation_data_create",
    # application state
    "utils", "app", "handlers", "preferences", "window_manager", "window", "screen", "area",
    "types", "props", "msgbus", "rna_type", "bl_rna", "id_data",
    # python modules and process access
    "os", "sys", "subprocess", "shutil", "pathlib", "importlib", "builtins", "io", "socket",
    "ctypes", "system", "popen", "exec", "eval", "compile", "open", "getattr", "setattr",
    "delattr", "globals", "locals", "vars",
    # str.format / format_map resolve attribute chains from a string literal (for example
    # "{0.__globals__[os]}"), which no AST rule over names can see.
    "format", "format_map",
})

# String literals equal to one of these are refused (they name data blocks or properties
# that could be reached through a mapping lookup such as ``bpy.context.copy()[...]``).
FORBIDDEN_STRINGS = frozenset({
    "texts", "blend_data", "libraries", "window_manager", "preferences", "filepath", "filepath_raw",
    "as_module", "utils", "app", "handlers",
})


def kit_public_names(kit_source: str) -> frozenset[str]:
    """Public top-level defs, classes and constants of a kit module (imports excluded)."""
    try:
        tree = ast.parse(kit_source or "")
    except (SyntaxError, ValueError):
        return frozenset()
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return frozenset(name for name in names if not name.startswith("_") and name not in FORBIDDEN_ATTRIBUTES)


def _private(name: str) -> bool:
    # A bare ``_`` is the conventional throw-away loop variable and grants no
    # access to anything; every other leading-underscore name is refused.
    return name.startswith("_") and name != "_"


def _line_key(reason: str) -> tuple[int, str]:
    head = reason.split(":", 1)[0].replace("line", "").strip()
    return (int(head) if head.isdigit() else 0, reason)


def check_script(source: str, kit_module: str | None = None,
                 kit_names: frozenset[str] | set[str] | None = None) -> list[str]:
    """Return human-readable rejection reasons; an empty list means accepted.

    ``kit_names`` is the set of kit attributes a script may touch (see
    :func:`kit_public_names`); with a kit module but no names, no kit attribute is allowed.
    """
    if not isinstance(source, str) or not source.strip():
        return ["script is empty"]
    if len(source) > MAX_SOURCE_CHARS:
        return ["script is %d characters; the limit is %d" % (len(source), MAX_SOURCE_CHARS)]
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return ["line %s: syntax error: %s" % (exc.lineno or "?", exc.msg)]
    except ValueError as exc:  # e.g. null bytes
        return ["script cannot be parsed: %s" % exc]

    allowed_roots = set(ALLOWED_IMPORT_ROOTS)
    if kit_module:
        allowed_roots.add(kit_module)
    kit_allowed = frozenset(kit_names or ())

    reasons: list[str] = []
    seen: set[str] = set()

    def reject(node: ast.AST, text: str) -> None:
        reason = "line %s: %s" % (getattr(node, "lineno", "?"), text)
        if reason not in seen:
            seen.add(reason)
            reasons.append(reason)

    parents: dict[int, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[id(child)] = parent

    kit_aliases: set[str] = set()
    if not any(isinstance(stmt, ast.FunctionDef) and stmt.name == "build" for stmt in tree.body):
        reasons.append("no top-level 'def build():' found")

    # ---- imports -------------------------------------------------------------------
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for item in node.names:
                root = item.name.split(".")[0]
                if root not in allowed_roots:
                    reject(node, "import of '%s' is not allowed (allowed: %s)"
                           % (item.name, ", ".join(sorted(allowed_roots))))
                elif root == "bpy" and (item.name != "bpy" or item.asname):
                    reject(node, "only a plain 'import bpy' is allowed (no submodule or alias)")
                elif kit_module and item.name == kit_module:
                    kit_aliases.add(item.asname or item.name)
                elif kit_module and root == kit_module:
                    reject(node, "import of '%s' is not allowed" % item.name)
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if node.level:
                reject(node, "relative imports are not allowed")
            elif root not in allowed_roots:
                reject(node, "import from '%s' is not allowed (allowed: %s)"
                       % (node.module, ", ".join(sorted(allowed_roots))))
            elif root == "bpy":
                reject(node, "'from bpy import ...' is not allowed; use 'import bpy' and spell the path out")
            elif kit_module and root == kit_module:
                if node.module != kit_module:
                    reject(node, "import from '%s' is not allowed" % node.module)
                for item in node.names:
                    if item.name == "*" or item.name not in kit_allowed:
                        reject(node, "'%s' is not a public name of the kit" % item.name)

    # ---- structural rules for bpy / kit roots -----------------------------------------
    def attr_parent(node: ast.AST) -> ast.Attribute | None:
        parent = parents.get(id(node))
        return parent if isinstance(parent, ast.Attribute) and parent.value is node else None

    def check_bpy(name_node: ast.Name) -> None:
        first = attr_parent(name_node)
        if first is None or first.attr not in ("ops", "data", "context"):
            reject(name_node, "'bpy' may only be used as bpy.ops.<category>.<op>(...), "
                              "bpy.data.<collection> or bpy.context.<member>; it cannot be bound, "
                              "passed, stored or returned")
            return
        second = attr_parent(first)
        if first.attr == "ops":
            if second is None or second.attr not in ALLOWED_OPS_CATEGORIES:
                reject(first, "bpy.ops may only be followed by one of: %s"
                       % ", ".join(sorted(ALLOWED_OPS_CATEGORIES)))
                return
            third = attr_parent(second)
            if third is None:
                reject(second, "bpy.ops.%s must be followed by an operator call" % second.attr)
                return
            if any(fragment in third.attr.lower() for fragment in FORBIDDEN_OPERATOR_FRAGMENTS):
                reject(third, "operator 'bpy.ops.%s.%s' is not allowed" % (second.attr, third.attr))
            call = parents.get(id(third))
            if not (isinstance(call, ast.Call) and call.func is third):
                reject(third, "bpy.ops.%s.%s must be called directly" % (second.attr, third.attr))
        elif first.attr == "data":
            if second is None or second.attr not in ALLOWED_DATA_COLLECTIONS:
                reject(first, "bpy.data may only be followed by one of: %s"
                       % ", ".join(sorted(ALLOWED_DATA_COLLECTIONS)))
        elif second is None or second.attr not in ALLOWED_CONTEXT_MEMBERS:
            reject(first, "bpy.context may only be followed by one of: %s"
                   % ", ".join(sorted(ALLOWED_CONTEXT_MEMBERS)))

    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            if node.id == "bpy":
                if isinstance(node.ctx, ast.Load):
                    check_bpy(node)
                else:
                    reject(node, "'bpy' cannot be rebound")
            elif node.id in kit_aliases:
                parent = attr_parent(node)
                if not isinstance(node.ctx, ast.Load) or parent is None or parent.attr not in kit_allowed:
                    reject(node, "the kit '%s' may only be used through its public names" % node.id)
            if node.id in FORBIDDEN_NAMES:
                reject(node, "use of '%s' is not allowed" % node.id)
            elif _private(node.id):
                reject(node, "names starting with '_' are not allowed ('%s')" % node.id)
        elif isinstance(node, ast.Attribute):
            if _private(node.attr):
                reject(node, "attributes starting with '_' are not allowed ('.%s')" % node.attr)
            elif node.attr in FORBIDDEN_ATTRIBUTES:
                reject(node, "attribute '.%s' is not allowed" % node.attr)
        elif isinstance(node, ast.keyword):
            if node.arg and (node.arg in FORBIDDEN_ATTRIBUTES or _private(node.arg)):
                reject(node, "keyword argument '%s' is not allowed" % node.arg)
        elif isinstance(node, ast.Constant):
            if isinstance(node.value, str) and node.value in FORBIDDEN_STRINGS:
                reject(node, "the string '%s' is not allowed" % node.value)
            elif isinstance(node.value, str) and "__" in node.value:
                reject(node, "string literals containing '__' are not allowed")
            elif isinstance(node.value, bytes) and b"__" in node.value:
                reject(node, "byte literals containing '__' are not allowed")
        elif isinstance(node, ast.arg):
            if _private(node.arg) or node.arg == "bpy" or node.arg in kit_aliases:
                reject(node, "parameter name '%s' is not allowed" % node.arg)
        elif isinstance(node, ast.alias):
            if node.asname and (node.asname == "bpy" or _private(node.asname)):
                reject(node, "alias '%s' is not allowed" % node.asname)
        elif isinstance(node, (ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef)):
            if node.name == "bpy" or _private(node.name):
                reject(node, "definition name '%s' is not allowed" % node.name)
        elif isinstance(node, ast.ExceptHandler):
            if node.name == "bpy" or (node.name and _private(node.name)):
                reject(node, "name '%s' is not allowed" % node.name)

    return sorted(reasons, key=_line_key)


__all__ = [
    "ALLOWED_DATA_COLLECTIONS", "ALLOWED_IMPORT_ROOTS", "ALLOWED_OPS_CATEGORIES", "FORBIDDEN_ATTRIBUTES",
    "FORBIDDEN_NAMES", "MAX_SOURCE_CHARS", "check_script", "kit_public_names",
]
