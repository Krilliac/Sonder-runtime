"""AST gate for model-written Blender build scripts.

This is defence in depth, not a sandbox: the script later runs in a headless
Blender with restricted builtins, and a determined author can still do harm
with ``bpy`` itself.  The gate keeps honest mistakes and obvious escapes out
(file/process/network access, dunder tricks, dynamic code, Blender operators
that touch files or preferences).  Pure; stdlib only.
"""
from __future__ import annotations

import ast

MAX_SOURCE_CHARS = 60_000

ALLOWED_IMPORT_ROOTS = frozenset({"bpy", "bmesh", "mathutils", "math", "random"})

FORBIDDEN_NAMES = frozenset({
    "open", "exec", "eval", "compile", "__import__", "globals", "locals", "vars",
    "getattr", "setattr", "delattr", "input", "breakpoint", "memoryview", "help",
    "exit", "quit",
})

# Dotted prefixes that may not be reached (after resolving import aliases).
FORBIDDEN_CHAINS = (
    "bpy.ops.wm", "bpy.ops.script", "bpy.ops.preferences", "bpy.ops.text",
    "bpy.ops.file", "bpy.ops.export_scene", "bpy.ops.import_scene",
    "bpy.ops.render", "bpy.app", "bpy.utils", "bpy.data.texts",
    "bpy.data.libraries", "bpy.context.preferences",
)


def _forbidden_chain(dotted: str) -> str | None:
    for prefix in FORBIDDEN_CHAINS:
        if dotted == prefix or dotted.startswith(prefix + "."):
            return prefix
    return None


def _chain(node: ast.AST, aliases: dict[str, str]) -> str | None:
    """Dotted path of ``a.b.c`` rooted at a plain name, aliases expanded."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    root = aliases.get(node.id, node.id)
    return ".".join([root] + list(reversed(parts)))


def _private(name: str) -> bool:
    # A bare ``_`` is the conventional throw-away loop variable and grants no
    # access to anything; every other leading-underscore name is refused.
    return name.startswith("_") and name != "_"


def check_script(source: str, kit_module: str | None = None) -> list[str]:
    """Return human-readable rejection reasons; an empty list means accepted."""
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

    allowed = set(ALLOWED_IMPORT_ROOTS)
    if kit_module:
        allowed.add(kit_module)

    reasons: list[str] = []
    seen: set[str] = set()

    def reject(node: ast.AST, text: str) -> None:
        reason = "line %s: %s" % (getattr(node, "lineno", "?"), text)
        if reason not in seen:
            seen.add(reason)
            reasons.append(reason)

    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for item in node.names:
                if item.name.split(".")[0] == "bpy":
                    if item.asname:
                        aliases[item.asname] = item.name
                    else:
                        aliases.setdefault("bpy", "bpy")
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            if node.module.split(".")[0] == "bpy":
                for item in node.names:
                    aliases[item.asname or item.name] = node.module + "." + item.name

    has_build = False
    for stmt in tree.body:
        if isinstance(stmt, ast.FunctionDef) and stmt.name == "build":
            has_build = True
    if not has_build:
        reasons.append("no top-level 'def build():' found")

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for item in node.names:
                if item.name.split(".")[0] not in allowed:
                    reject(node, "import of '%s' is not allowed (allowed: %s)"
                           % (item.name, ", ".join(sorted(allowed))))
                else:
                    hit = _forbidden_chain(item.name)
                    if hit:
                        reject(node, "import of '%s' is not allowed" % item.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                reject(node, "relative imports are not allowed")
            elif (node.module or "").split(".")[0] not in allowed:
                reject(node, "import from '%s' is not allowed (allowed: %s)"
                       % (node.module, ", ".join(sorted(allowed))))
            else:
                hit = _forbidden_chain(node.module or "")
                if hit:
                    reject(node, "import from '%s' is not allowed" % node.module)
        elif isinstance(node, ast.Name):
            if node.id in FORBIDDEN_NAMES:
                reject(node, "use of '%s' is not allowed" % node.id)
            elif _private(node.id):
                reject(node, "names starting with '_' are not allowed ('%s')" % node.id)
        elif isinstance(node, ast.arg):
            if _private(node.arg):
                reject(node, "names starting with '_' are not allowed ('%s')" % node.arg)
        elif isinstance(node, ast.Attribute):
            if _private(node.attr):
                reject(node, "attributes starting with '_' are not allowed ('.%s')" % node.attr)
            elif node.attr in FORBIDDEN_NAMES:
                reject(node, "use of '.%s' is not allowed" % node.attr)
            dotted = _chain(node, aliases)
            if dotted:
                hit = _forbidden_chain(dotted)
                if hit:
                    reject(node, "access to '%s' is not allowed" % hit)

    return sorted(reasons, key=_line_key)


def _line_key(reason: str) -> tuple[int, str]:
    head = reason.split(":", 1)[0]
    digits = head.replace("line", "").strip()
    return (int(digits) if digits.isdigit() else 0, reason)


__all__ = ["ALLOWED_IMPORT_ROOTS", "FORBIDDEN_CHAINS", "FORBIDDEN_NAMES", "MAX_SOURCE_CHARS", "check_script"]
