"""Guarded, bounded, stdlib-only API, import, signature, and repository symbol verifier.

Directly targets one of the most common LLM hallucinations: inventing
nonexistent Python modules, attributes, methods, keyword arguments, or
repository symbols.

Security & safety invariants:
- Never executes untrusted third-party modules: checks module existence with
  ``importlib.util.find_spec`` and inspects ``.py`` source files via ``ast.parse``
  without importing them.
- Uses ``importlib.import_module`` + ``inspect.signature`` ONLY on a closed
  allowlist of safe Python standard-library modules (to cover C-extension
  builtins such as ``math``, ``json``, ``re``, ``sqlite3``, ``hashlib``).
- Any repository file access enforces ``file_ops.require_read_access`` and
  refuses symlinks.
"""
from __future__ import annotations

import ast
import importlib
import importlib.util
import inspect
import os
from pathlib import Path
import sys
from typing import Any

import sonder_runtime.adapters.filesystem.file_ops as file_ops

MAX_CODE_CHARS = 64_000
MAX_FILE_BYTES = 1 * 1024 * 1024

# Closed allowlist of safe stdlib modules that may be inspected in-process for
# C-extension attributes and signatures (zero side effects on import).
_SAFE_STDLIB_INSPECT = frozenset({
    "abc", "argparse", "array", "ast", "asyncio", "base64", "binascii",
    "bisect", "builtins", "calendar", "cmath", "collections", "colorsys",
    "configparser", "contextlib", "contextvars", "copy", "csv", "dataclasses",
    "datetime", "decimal", "difflib", "email", "enum", "errno", "fnmatch",
    "fractions", "functools", "gc", "glob", "gzip", "hashlib", "heapq",
    "hmac", "html", "http", "inspect", "io", "ipaddress", "itertools", "json",
    "keyword", "logging", "math", "mimetypes", "numbers", "operator", "os",
    "pathlib", "pickle", "platform", "posixpath", "pprint", "queue", "random",
    "re", "secrets", "shlex", "shutil", "signal", "socket", "sqlite3", "ssl",
    "stat", "statistics", "string", "struct", "subprocess", "sys", "tarfile",
    "tempfile", "textwrap", "threading", "time", "timeit", "tomllib", "traceback",
    "types", "typing", "unicodedata", "unittest", "urllib", "uuid", "warnings",
    "weakref", "xml", "zipfile", "zlib",
})

_STDLIB_NAMES: frozenset[str] = frozenset(getattr(sys, "stdlib_module_names", ()))


def _ast_module_exports(source_path: Path) -> tuple[set[str], dict[str, dict[str, Any]]]:
    """Parse a Python source file statically via AST without executing it."""
    try:
        if source_path.stat().st_size > MAX_FILE_BYTES:
            return set(), {}
        text = source_path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(text)
    except (OSError, SyntaxError, ValueError, RecursionError):
        return set(), {}

    exports: set[str] = set()
    signatures: dict[str, dict[str, Any]] = {}

    def _params_from_args(args: ast.arguments, *, drop_self: bool = False) -> dict[str, Any]:
        pos = [a.arg for a in args.posonlyargs] + [a.arg for a in args.args]
        if drop_self and pos and pos[0] in {"self", "cls"}:
            pos = pos[1:]
        kwonly = [a.arg for a in args.kwonlyargs]
        return {
            "positional": pos,
            "kwonly": kwonly,
            "all_named": set(pos) | set(kwonly),
            "has_varargs": args.vararg is not None,
            "has_varkw": args.kwarg is not None,
            "signature": ast.unparse(args),
        }

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            exports.add(node.name)
            signatures[node.name] = {
                "kind": "function",
                "line": node.lineno,
                **_params_from_args(node.args),
            }
        elif isinstance(node, ast.ClassDef):
            exports.add(node.name)
            methods: dict[str, dict[str, Any]] = {}
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    methods[item.name] = {
                        "kind": "method",
                        "line": item.lineno,
                        **_params_from_args(item.args, drop_self=True),
                    }
            init_sig = methods.get("__init__")
            signatures[node.name] = {
                "kind": "class",
                "line": node.lineno,
                "methods": sorted(methods.keys()),
                "method_sigs": methods,
                "all_named": init_sig["all_named"] if init_sig else set(),
                "has_varkw": init_sig["has_varkw"] if init_sig else False,
            }
        elif isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name):
                    exports.add(tgt.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            exports.add(node.target.id)
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name != "*":
                    exports.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                exports.add(alias.asname or alias.name.split(".", 1)[0])
    return exports, signatures


def _inspect_safe_stdlib_attr(module_name: str, attr: str = "") -> dict[str, Any]:
    top = module_name.split(".", 1)[0]
    if top not in _SAFE_STDLIB_INSPECT:
        return {"inspected": False}
    try:
        mod = importlib.import_module(module_name)
    except Exception:
        return {"inspected": False}
    if not attr:
        public = sorted(name for name in dir(mod) if not name.startswith("_"))[:60]
        return {"inspected": True, "exists": True, "module": module_name, "sample_exports": public}

    parts = [p for p in attr.split(".") if p]
    cur: Any = mod
    for part in parts:
        if not hasattr(cur, part):
            # Suggest close matches to help self-repair
            candidates = [n for n in dir(cur) if not n.startswith("_")]
            close = [n for n in candidates if part.lower() in n.lower() or n.lower() in part.lower()][:8]
            return {
                "inspected": True,
                "exists": False,
                "missing_part": part,
                "suggestions": close,
            }
        cur = getattr(cur, part)

    sig_info: dict[str, Any] = {"inspected": True, "exists": True, "type": type(cur).__name__}
    if callable(cur):
        try:
            sig = inspect.signature(cur)
            params = []
            accepts_varkw = False
            kw_names: list[str] = []
            for p in sig.parameters.values():
                params.append(str(p))
                if p.kind == inspect.Parameter.VAR_KEYWORD:
                    accepts_varkw = True
                elif p.kind in (
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                    inspect.Parameter.KEYWORD_ONLY,
                ):
                    kw_names.append(p.name)
            sig_info["signature"] = str(sig)
            sig_info["parameters"] = params
            sig_info["keyword_names"] = kw_names
            sig_info["accepts_varkw"] = accepts_varkw
        except (ValueError, TypeError):
            sig_info["signature"] = "(builtin/opaque)"
    return sig_info


def _resolve_repo_module(module_name: str, root: Path) -> Path | None:
    parts = [p for p in module_name.split(".") if p and p not in {".", ".."}]
    if not parts:
        return None
    cand_file = root.joinpath(*parts).with_suffix(".py")
    cand_pkg = root.joinpath(*parts, "__init__.py")
    for cand in (cand_file, cand_pkg):
        try:
            resolved = cand.resolve()
            if (resolved == root or root in resolved.parents) and not cand.is_symlink() and resolved.is_file():
                return resolved
        except OSError:
            continue
    return None


def verify_python_symbol(
    module: str,
    *,
    symbol: str = "",
    kwargs_to_check: list[str] | None = None,
    root: str = "",
    extra_roots: str = "",
) -> dict[str, Any]:
    """Verify whether a Python module, attribute/method, and optional kwargs actually exist."""
    mod_name = str(module or "").strip()
    if not mod_name:
        return {"ok": False, "error": "module is required"}
    if not all(part.isidentifier() for part in mod_name.split(".")):
        return {"ok": False, "error": f"invalid Python module path: {mod_name!r}"}

    repo_root: Path | None = None
    if root:
        repo_root = file_ops.require_read_access(root, extra_roots=extra_roots)
        if repo_root.is_symlink():
            raise PermissionError("refusing symlink root")

    # 1. Check repository module first when root is supplied
    if repo_root is not None:
        repo_file = _resolve_repo_module(mod_name, repo_root)
        if repo_file is not None:
            exports, sigs = _ast_module_exports(repo_file)
            if not symbol:
                return {
                    "ok": True,
                    "verified": True,
                    "source": "repository",
                    "module": mod_name,
                    "file": repo_file.relative_to(repo_root).as_posix(),
                    "exports": sorted(exports)[:60],
                }
            parts = symbol.split(".")
            head = parts[0]
            if head not in exports:
                close = [n for n in sorted(exports) if head.lower() in n.lower() or n.lower() in head.lower()][:8]
                return {
                    "ok": True,
                    "verified": False,
                    "source": "repository",
                    "module": mod_name,
                    "symbol": symbol,
                    "reason": f"symbol {head!r} not defined in {repo_file.relative_to(repo_root).as_posix()}",
                    "suggestions": close,
                }
            target_sig = sigs.get(head)
            if len(parts) == 2 and target_sig and target_sig.get("kind") == "class":
                method_name = parts[1]
                msigs = target_sig.get("method_sigs") or {}
                if method_name not in msigs:
                    return {
                        "ok": True,
                        "verified": False,
                        "source": "repository",
                        "module": mod_name,
                        "symbol": symbol,
                        "reason": f"method {method_name!r} not found on class {head!r}",
                        "suggestions": target_sig.get("methods", [])[:12],
                    }
                target_sig = msigs[method_name]
            bad_kwargs: list[str] = []
            if kwargs_to_check and target_sig and not target_sig.get("has_varkw"):
                allowed = target_sig.get("all_named") or set()
                bad_kwargs = [k for k in kwargs_to_check if k not in allowed]
            return {
                "ok": True,
                "verified": len(bad_kwargs) == 0,
                "source": "repository",
                "module": mod_name,
                "symbol": symbol,
                "signature": target_sig.get("signature") if target_sig else None,
                "invalid_kwargs": bad_kwargs,
            }

    # 2. Safe stdlib in-process inspection (handles C extensions cleanly)
    top = mod_name.split(".", 1)[0]
    if top in _SAFE_STDLIB_INSPECT:
        info = _inspect_safe_stdlib_attr(mod_name, symbol)
        if info.get("inspected"):
            exists = bool(info.get("exists"))
            bad_kwargs = []
            if exists and kwargs_to_check and "keyword_names" in info and not info.get("accepts_varkw"):
                allowed_kw = set(info["keyword_names"])
                bad_kwargs = [k for k in kwargs_to_check if k not in allowed_kw]
            out: dict[str, Any] = {
                "ok": True,
                "verified": exists and len(bad_kwargs) == 0,
                "source": "stdlib",
                "module": mod_name,
                "symbol": symbol,
                **{k: v for k, v in info.items() if k != "inspected"},
            }
            if bad_kwargs:
                out["invalid_kwargs"] = bad_kwargs
                out["reason"] = f"invalid keyword argument(s): {', '.join(bad_kwargs)}"
            elif not exists:
                out["reason"] = f"attribute {symbol!r} does not exist on stdlib module {mod_name!r}"
            return out

    # 3. Installed third-party/other stdlib check via find_spec + AST (never executes module)
    try:
        spec = importlib.util.find_spec(mod_name)
    except (ModuleNotFoundError, ValueError, ImportError):
        spec = None
    if spec is None:
        return {
            "ok": True,
            "verified": False,
            "source": "environment",
            "module": mod_name,
            "symbol": symbol,
            "reason": f"module {mod_name!r} is not installed or does not exist in Python {sys.version_info.major}.{sys.version_info.minor}",
        }
    origin = getattr(spec, "origin", None)
    if not symbol:
        return {
            "ok": True,
            "verified": True,
            "source": "stdlib" if top in _STDLIB_NAMES else "installed_package",
            "module": mod_name,
            "has_source": bool(origin and str(origin).endswith(".py")),
        }
    if origin and str(origin).endswith(".py") and os.path.isfile(origin):
        exports, sigs = _ast_module_exports(Path(origin))
        head = symbol.split(".", 1)[0]
        if head in exports:
            tsig = sigs.get(head)
            bad_kwargs = []
            if kwargs_to_check and tsig and not tsig.get("has_varkw"):
                allowed = tsig.get("all_named") or set()
                bad_kwargs = [k for k in kwargs_to_check if k not in allowed]
            return {
                "ok": True,
                "verified": len(bad_kwargs) == 0,
                "source": "ast_installed",
                "module": mod_name,
                "symbol": symbol,
                "signature": tsig.get("signature") if tsig else None,
                "invalid_kwargs": bad_kwargs,
            }
        # Re-exported dynamically via __all__ or C extension; report inconclusive rather than false-positive
        return {
            "ok": True,
            "verified": False,
            "source": "ast_installed",
            "module": mod_name,
            "symbol": symbol,
            "reason": f"symbol {head!r} not statically found in {mod_name}",
            "suggestions": [n for n in sorted(exports) if head.lower() in n.lower()][:8],
        }
    return {
        "ok": True,
        "verified": None,
        "source": "binary_extension",
        "module": mod_name,
        "symbol": symbol,
        "reason": "module exists as compiled extension outside safe inspect allowlist",
    }


def audit_python_code(
    code: str,
    *,
    root: str = "",
    extra_roots: str = "",
) -> dict[str, Any]:
    """Statically audit a Python snippet for hallucinated imports, attributes, and invalid kwargs."""
    raw = str(code or "")
    if not raw.strip():
        return {"ok": False, "error": "code is required"}
    if len(raw) > MAX_CODE_CHARS:
        return {"ok": False, "error": f"code exceeds {MAX_CODE_CHARS} characters"}
    try:
        tree = ast.parse(raw)
    except (SyntaxError, ValueError, RecursionError) as exc:
        return {
            "ok": True,
            "clean": False,
            "issues": [{"kind": "syntax_error", "line": getattr(exc, "lineno", None), "detail": str(exc)}],
        }

    repo_root: Path | None = None
    if root:
        repo_root = file_ops.require_read_access(root, extra_roots=extra_roots)
        if repo_root.is_symlink():
            raise PermissionError("refusing symlink root")

    # Track imported module aliases and direct symbol imports
    mod_aliases: dict[str, str] = {}
    direct_imports: dict[str, tuple[str, str]] = {}
    issues: list[dict[str, Any]] = []
    checked_count = 0

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                bound = alias.asname or alias.name.split(".", 1)[0]
                mod_aliases[bound] = alias.name if alias.asname else alias.name.split(".", 1)[0]
                checked_count += 1
                res = verify_python_symbol(alias.name, root=str(repo_root) if repo_root else "", extra_roots=extra_roots)
                if res.get("verified") is False:
                    issues.append({
                        "kind": "hallucinated_import",
                        "line": node.lineno,
                        "module": alias.name,
                        "detail": res.get("reason", f"module {alias.name!r} not found"),
                    })
        elif isinstance(node, ast.ImportFrom):
            if (node.level or 0) > 0 or not node.module:
                continue
            mod_name = node.module
            for alias in node.names:
                if alias.name == "*":
                    checked_count += 1
                    res = verify_python_symbol(mod_name, root=str(repo_root) if repo_root else "", extra_roots=extra_roots)
                    if res.get("verified") is False:
                        issues.append({
                            "kind": "hallucinated_import",
                            "line": node.lineno,
                            "module": mod_name,
                            "detail": res.get("reason", f"module {mod_name!r} not found"),
                        })
                    continue
                bound = alias.asname or alias.name
                # Could be importing a submodule (from os import path) or attribute (from math import sqrt)
                sub_res = verify_python_symbol(f"{mod_name}.{alias.name}", root=str(repo_root) if repo_root else "", extra_roots=extra_roots)
                if sub_res.get("verified") is True:
                    mod_aliases[bound] = f"{mod_name}.{alias.name}"
                    checked_count += 1
                    continue
                checked_count += 1
                attr_res = verify_python_symbol(mod_name, symbol=alias.name, root=str(repo_root) if repo_root else "", extra_roots=extra_roots)
                if attr_res.get("verified") is False:
                    issues.append({
                        "kind": "hallucinated_symbol",
                        "line": node.lineno,
                        "module": mod_name,
                        "symbol": alias.name,
                        "detail": attr_res.get("reason", f"{mod_name}.{alias.name} does not exist"),
                        "suggestions": attr_res.get("suggestions", []),
                    })
                else:
                    direct_imports[bound] = (mod_name, alias.name)

    # Check attribute calls `mod.attr(...)` and direct imported calls `fn(bad_kw=...)`
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        kw_names = [kw.arg for kw in node.keywords if kw.arg is not None]
        if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
            base = node.func.value.id
            if base in mod_aliases:
                mod_name = mod_aliases[base]
                attr = node.func.attr
                checked_count += 1
                res = verify_python_symbol(
                    mod_name,
                    symbol=attr,
                    kwargs_to_check=kw_names,
                    root=str(repo_root) if repo_root else "",
                    extra_roots=extra_roots,
                )
                if res.get("verified") is False:
                    issues.append({
                        "kind": "hallucinated_kwarg" if res.get("invalid_kwargs") else "hallucinated_attribute",
                        "line": node.lineno,
                        "module": mod_name,
                        "symbol": attr,
                        "detail": res.get("reason", f"{mod_name}.{attr} check failed"),
                        "suggestions": res.get("suggestions", []),
                    })
        elif isinstance(node.func, ast.Name) and node.func.id in direct_imports and kw_names:
            mod_name, sym_name = direct_imports[node.func.id]
            checked_count += 1
            res = verify_python_symbol(
                mod_name,
                symbol=sym_name,
                kwargs_to_check=kw_names,
                root=str(repo_root) if repo_root else "",
                extra_roots=extra_roots,
            )
            if res.get("verified") is False and res.get("invalid_kwargs"):
                issues.append({
                    "kind": "hallucinated_kwarg",
                    "line": node.lineno,
                    "module": mod_name,
                    "symbol": sym_name,
                    "detail": res.get("reason", "invalid keyword arguments"),
                })

    # Deduplicate issues
    seen: set[tuple[Any, ...]] = set()
    deduped: list[dict[str, Any]] = []
    for item in issues:
        key = (item.get("kind"), item.get("line"), item.get("module"), item.get("symbol"), item.get("detail"))
        if key not in seen:
            seen.add(key)
            deduped.append(item)

    return {
        "ok": True,
        "clean": len(deduped) == 0,
        "checked_count": checked_count,
        "issue_count": len(deduped),
        "issues": deduped[:50],
    }


def symbol_verify(
    *,
    action: str = "verify_symbol",
    module: str = "",
    symbol: str = "",
    kwargs: list[str] | None = None,
    code: str = "",
    root: str = "",
    extra_roots: str = "",
) -> dict[str, Any]:
    """Verify Python modules/attributes/kwargs or statically audit a code snippet for hallucinated APIs."""
    act = str(action or "verify_symbol").strip().lower() or "verify_symbol"
    try:
        if act == "audit_code" or (not module and code):
            return audit_python_code(code, root=root, extra_roots=extra_roots)
        if act == "verify_symbol":
            return verify_python_symbol(
                module,
                symbol=symbol,
                kwargs_to_check=kwargs,
                root=root,
                extra_roots=extra_roots,
            )
        return {"ok": False, "error": f"unknown action {act!r} (use verify_symbol or audit_code)"}
    except (OSError, PermissionError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}


__all__ = ["audit_python_code", "symbol_verify", "verify_python_symbol"]
