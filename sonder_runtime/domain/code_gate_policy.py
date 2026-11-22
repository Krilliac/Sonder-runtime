"""Pure policy for selecting replies that need the chat code gate."""

from __future__ import annotations

import ast
import builtins
import re
from collections.abc import Callable

CODE_GATE_SIGNS = re.compile(
    r"^\s*(?:def\s+\w+|class\s+\w+|import\s+\w+|from\s+[\w.]+\s+import\s)",
    re.MULTILINE,
)

_BUILTIN_NAMES: frozenset[str] = frozenset(dir(builtins))


def code_gate_target(
    reply: str,
    extract_runnable_code_block: Callable[[str], dict | None],
) -> str | None:
    """Return runnable Python code worth compiling and smoke-running, or ``None``.

    The extractor is injected because it is an infrastructure concern owned by
    the legacy grounding module. This function owns only the gate's pure
    selection rules: fenced Python, meaningful definitions/imports, and no
    interactive stdin dependency.
    """
    if "```" not in str(reply or ""):
        return None
    block = extract_runnable_code_block(reply)
    if not block or block.get("language") != "python":
        return None
    code = block.get("code") or ""
    if not CODE_GATE_SIGNS.search(code):
        return None
    if re.search(r"\binput\s*\(", code):
        return None
    return code


def code_gate_compile_only_target(
    reply: str,
    extract_runnable_code_block: Callable[[str], dict | None],
) -> str | None:
    """Return interactive fenced Python code (using ``input()``) for static verification."""
    if "```" not in str(reply or ""):
        return None
    block = extract_runnable_code_block(reply)
    if not block or block.get("language") != "python":
        return None
    code = block.get("code") or ""
    if not CODE_GATE_SIGNS.search(code):
        return None
    if not re.search(r"\binput\s*\(", code):
        return None
    return code


def check_python_static_errors(code: str) -> str | None:
    """Compile and statically check Python code for syntax and undefined function calls.

    Designed for zero false positives on valid self-contained scripts:
    - Catches every ``SyntaxError`` / ``IndentationError`` via ``compile()``.
    - Catches bare ``foo(...)`` calls where ``foo`` is not a builtin and is never
      defined, imported, or assigned anywhere in the module (skipped if wildcard
      ``from ... import *`` is present).
    """
    try:
        compile(code, "<reply>", "exec")
        tree = ast.parse(code)
    except (SyntaxError, ValueError, OverflowError) as exc:
        return f"compile failed\n{type(exc).__name__}: {exc}"

    defined: set[str] = set(_BUILTIN_NAMES) | {
        "__name__", "__file__", "__doc__", "__package__", "__spec__",
        "__builtins__", "__annotations__",
    }
    has_star_import = False
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defined.add(node.name)
        elif isinstance(node, ast.arg):
            defined.add(node.arg)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                bound = alias.asname or alias.name.split(".", 1)[0]
                defined.add(bound)
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "*":
                    has_star_import = True
                else:
                    defined.add(alias.asname or alias.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            defined.add(node.id)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            defined.add(node.name)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            defined.update(node.names)

    if has_star_import:
        return None

    missing: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            name = node.func.id
            if name not in defined and name not in missing:
                missing.append(name)
    if missing:
        names = ", ".join(repr(m) for m in missing[:5])
        return f"NameError: undefined function call(s): {names}"
    return None
