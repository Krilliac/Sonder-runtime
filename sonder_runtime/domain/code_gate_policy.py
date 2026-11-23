"""Pure policy for selecting replies that need the chat code gate."""

from __future__ import annotations

import ast
import builtins
import importlib
import inspect
import re
from collections.abc import Callable

CODE_GATE_SIGNS = re.compile(
    r"^\s*(?:def\s+\w+|class\s+\w+|import\s+\w+|from\s+[\w.]+\s+import\s)",
    re.MULTILINE,
)

_BUILTIN_NAMES: frozenset[str] = frozenset(dir(builtins))
_MATCH_NAME_NODES = tuple(
    node_cls
    for node_cls in (getattr(ast, "MatchAs", None), getattr(ast, "MatchStar", None))
    if node_cls is not None
)
_MATCH_MAPPING_NODE = getattr(ast, "MatchMapping", None)


def uses_interactive_stdin(code: str) -> bool:
    """Detect interactive stdin calls via AST (``input``, ``getpass.getpass``, ``sys.stdin.read*``).

    Ignores comments, string literals, and unrelated method calls like ``obj.input()``.
    Returns ``False`` when ``code`` does not parse so syntax errors are still caught by
    the normal code gate rather than silently skipped.
    """
    try:
        tree = ast.parse(str(code or ""))
    except (SyntaxError, ValueError, OverflowError, RecursionError):
        return False

    builtins_modules: set[str] = {"builtins"}
    getpass_modules: set[str] = {"getpass"}
    getpass_funcs: set[str] = set()
    sys_modules: set[str] = {"sys"}
    stdin_names: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                bound = alias.asname or alias.name.split(".", 1)[0]
                if alias.name == "builtins":
                    builtins_modules.add(bound)
                elif alias.name == "getpass":
                    getpass_modules.add(bound)
                elif alias.name == "sys":
                    sys_modules.add(bound)
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            for alias in node.names:
                bound = alias.asname or alias.name
                if mod == "getpass" and alias.name == "getpass":
                    getpass_funcs.add(bound)
                elif mod == "sys" and alias.name == "stdin":
                    stdin_names.add(bound)

    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "stdin"
            and isinstance(node.value.value, ast.Name)
            and node.value.value.id in sys_modules
        ):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    stdin_names.add(target.id)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            if func.id == "input" or func.id in getpass_funcs:
                return True
        elif isinstance(func, ast.Attribute):
            if (
                func.attr == "input"
                and isinstance(func.value, ast.Name)
                and func.value.id in builtins_modules
            ):
                return True
            if (
                func.attr == "getpass"
                and isinstance(func.value, ast.Name)
                and func.value.id in getpass_modules
            ):
                return True
            if func.attr.startswith("read"):
                if isinstance(func.value, ast.Name) and func.value.id in stdin_names:
                    return True
                if (
                    isinstance(func.value, ast.Attribute)
                    and func.value.attr == "stdin"
                    and isinstance(func.value.value, ast.Name)
                    and func.value.value.id in sys_modules
                ):
                    return True
    return False


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
    if uses_interactive_stdin(code):
        return None
    return code


def code_gate_compile_only_target(
    reply: str,
    extract_runnable_code_block: Callable[[str], dict | None],
) -> str | None:
    """Return interactive fenced Python code (using interactive stdin) for static verification."""
    if "```" not in str(reply or ""):
        return None
    block = extract_runnable_code_block(reply)
    if not block or block.get("language") != "python":
        return None
    code = block.get("code") or ""
    if not CODE_GATE_SIGNS.search(code):
        return None
    if not uses_interactive_stdin(code):
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
    except (SyntaxError, ValueError, OverflowError, RecursionError) as exc:
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
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            defined.add(node.id)
        elif _MATCH_NAME_NODES and isinstance(node, _MATCH_NAME_NODES) and getattr(node, "name", None):
            defined.add(node.name)
        elif _MATCH_MAPPING_NODE is not None and isinstance(node, _MATCH_MAPPING_NODE) and getattr(node, "rest", None):
            defined.add(node.rest)
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
    return check_python_stdlib_api_errors(code)


_GUARDED_STDLIB_MODULES: frozenset[str] = frozenset({
    "ast", "asyncio", "base64", "bisect", "calendar", "cmath", "collections",
    "contextlib", "copy", "csv", "dataclasses", "datetime", "decimal",
    "difflib", "enum", "fractions", "functools", "glob", "gzip", "hashlib",
    "heapq", "hmac", "html", "inspect", "io", "ipaddress", "itertools", "json",
    "logging", "math", "operator", "os", "pathlib", "random", "re", "secrets",
    "shlex", "shutil", "sqlite3", "statistics", "string", "struct",
    "subprocess", "sys", "tempfile", "textwrap", "threading", "time",
    "tomllib", "typing", "uuid", "zipfile", "zlib",
})


def check_python_stdlib_api_errors(code: str) -> str | None:
    """Detect hallucinated attributes or invalid kwargs on safe Python stdlib modules.

    Purely static AST check over a closed allowlist of safe stdlib modules so it
    never executes user code and never false-positives on third-party packages.
    """
    try:
        tree = ast.parse(str(code or ""))
    except (SyntaxError, ValueError, OverflowError, RecursionError):
        return None

    mod_aliases: dict[str, str] = {}
    direct_imports: dict[str, tuple[str, str]] = {}
    errors: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".", 1)[0]
                if top in _GUARDED_STDLIB_MODULES:
                    bound = alias.asname or top
                    mod_aliases[bound] = alias.name if alias.asname else top
        elif isinstance(node, ast.ImportFrom) and (node.level or 0) == 0 and node.module:
            top = node.module.split(".", 1)[0]
            if top not in _GUARDED_STDLIB_MODULES:
                continue
            try:
                mod = importlib.import_module(node.module)
            except Exception:
                continue
            for alias in node.names:
                if alias.name == "*":
                    continue
                if not hasattr(mod, alias.name):
                    errors.append(f"ImportError: cannot import {alias.name!r} from stdlib module {node.module!r}")
                else:
                    direct_imports[alias.asname or alias.name] = (node.module, alias.name)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        kw_names = [kw.arg for kw in node.keywords if kw.arg is not None]
        if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
            base = node.func.value.id
            if base in mod_aliases:
                mod_name = mod_aliases[base]
                attr = node.func.attr
                try:
                    mod = importlib.import_module(mod_name)
                except Exception:
                    continue
                if not hasattr(mod, attr):
                    errors.append(f"AttributeError: stdlib module {mod_name!r} has no attribute {attr!r}")
                    continue
                target = getattr(mod, attr)
                if kw_names and callable(target):
                    try:
                        sig = inspect.signature(target)
                        has_varkw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
                        if not has_varkw:
                            allowed = {
                                p.name for p in sig.parameters.values()
                                if p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
                            }
                            bad = [k for k in kw_names if k not in allowed]
                            if bad:
                                errors.append(f"TypeError: {mod_name}.{attr}() got unexpected keyword argument(s): {', '.join(bad)}")
                    except (ValueError, TypeError):
                        pass
        elif isinstance(node.func, ast.Name) and node.func.id in direct_imports and kw_names:
            mod_name, sym = direct_imports[node.func.id]
            try:
                mod = importlib.import_module(mod_name)
                target = getattr(mod, sym, None)
            except Exception:
                target = None
            if callable(target):
                try:
                    sig = inspect.signature(target)
                    has_varkw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
                    if not has_varkw:
                        allowed = {
                            p.name for p in sig.parameters.values()
                            if p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
                        }
                        bad = [k for k in kw_names if k not in allowed]
                        if bad:
                            errors.append(f"TypeError: {mod_name}.{sym}() got unexpected keyword argument(s): {', '.join(bad)}")
                except (ValueError, TypeError):
                    pass

    if errors:
        return "; ".join(dict.fromkeys(errors[:4]))
    return None
