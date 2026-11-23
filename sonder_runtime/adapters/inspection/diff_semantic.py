"""Guarded, bounded, stdlib-only structural / AST-aware code and data diff.

Eliminates whitespace/formatting noise when comparing two versions of Python
code, JSON, TOML, INI, or brace-language source files (or two inline text
snippets):
- ``python``: AST-level diff of imports, classes, functions, parameter
  signatures, return annotations, decorators, and normalized AST body hashes
  (formatting & comment-only edits report zero behavioral change).
- ``json`` / ``toml`` / ``ini``: deep key-path diff listing ``added``,
  ``removed``, ``changed``, and ``type_changed`` leaves.
- ``generic``: conservative symbol-signature diff for C/C++/C#/JS/TS/Rust/Go/
  Dart/Java plus line-level change counts.
"""
from __future__ import annotations

import ast
import configparser
import hashlib
import json
from pathlib import Path
import re
import tomllib
from typing import Any

import sonder_runtime.adapters.filesystem.file_ops as file_ops

MAX_INPUT_BYTES = 1 * 1024 * 1024
MAX_DIFF_ITEMS = 200


def _read_side(path: str, text: str, *, extra_roots: str = "") -> tuple[str, str]:
    if path:
        resolved = file_ops.require_read_access(path, extra_roots=extra_roots)
        if resolved.is_symlink():
            raise PermissionError(f"refusing symlink: {path}")
        if not resolved.is_file():
            raise FileNotFoundError(f"not a file: {path}")
        size = resolved.stat().st_size
        if size > MAX_INPUT_BYTES:
            raise ValueError(f"file exceeds {MAX_INPUT_BYTES} byte limit: {path}")
        return resolved.read_text(encoding="utf-8", errors="replace"), resolved.suffix.lower()
    raw = str(text or "")
    if len(raw.encode("utf-8", errors="replace")) > MAX_INPUT_BYTES:
        raise ValueError(f"inline text exceeds {MAX_INPUT_BYTES} byte limit")
    return raw, ""


def _infer_kind(kind: str, ext_a: str, ext_b: str, text_a: str, text_b: str) -> str:
    k = str(kind or "auto").strip().lower() or "auto"
    if k != "auto":
        return k
    ext = ext_a or ext_b
    if ext in {".py", ".pyw"}:
        return "python"
    if ext == ".json":
        return "json"
    if ext == ".toml":
        return "toml"
    if ext in {".ini", ".cfg"}:
        return "ini"
    stripped = text_a.lstrip()
    if stripped.startswith(("{", "[")):
        try:
            json.loads(text_a)
            json.loads(text_b)
            return "json"
        except ValueError:
            pass
    if re.search(r"^\s*(?:def|async\s+def|class|import|from\s+\S+\s+import)\b", text_a, re.MULTILINE):
        return "python"
    return "generic"


def _fn_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    prefix = "async def " if isinstance(node, ast.AsyncFunctionDef) else "def "
    args_src = ast.unparse(node.args)
    ret = f" -> {ast.unparse(node.returns)}" if node.returns is not None else ""
    return f"{prefix}{node.name}({args_src}){ret}"


def _ast_body_hash(nodes: list[ast.stmt]) -> str:
    # Strip leading docstring expression so docstring-only tweaks are visible separately if desired,
    # while ignoring line numbers and formatting completely via ast.dump(include_attributes=False).
    dump = "\n".join(ast.dump(stmt, include_attributes=False) for stmt in nodes)
    return hashlib.sha256(dump.encode("utf-8")).hexdigest()[:16]


def _extract_py_structure(source: str) -> dict[str, Any]:
    tree = ast.parse(source)
    imports: set[str] = set()
    symbols: dict[str, dict[str, Any]] = {}

    def walk_body(stmts: list[ast.stmt], scope: list[str]) -> None:
        for stmt in stmts:
            if isinstance(stmt, ast.Import):
                for alias in stmt.names:
                    imports.add(f"import {alias.name}" + (f" as {alias.asname}" if alias.asname else ""))
            elif isinstance(stmt, ast.ImportFrom):
                mod = "." * (stmt.level or 0) + (stmt.module or "")
                for alias in stmt.names:
                    imports.add(f"from {mod} import {alias.name}" + (f" as {alias.asname}" if alias.asname else ""))
            elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qname = ".".join([*scope, stmt.name])
                decs = [ast.unparse(d) for d in stmt.decorator_list]
                symbols[qname] = {
                    "kind": "async_function" if isinstance(stmt, ast.AsyncFunctionDef) else "function",
                    "signature": _fn_signature(stmt),
                    "decorators": decs,
                    "body_hash": _ast_body_hash(stmt.body),
                    "line": stmt.lineno,
                }
            elif isinstance(stmt, ast.ClassDef):
                qname = ".".join([*scope, stmt.name])
                bases = [ast.unparse(b) for b in stmt.bases]
                decs = [ast.unparse(d) for d in stmt.decorator_list]
                symbols[qname] = {
                    "kind": "class",
                    "signature": f"class {stmt.name}({', '.join(bases)})" if bases else f"class {stmt.name}",
                    "decorators": decs,
                    "body_hash": _ast_body_hash(stmt.body),
                    "line": stmt.lineno,
                }
                walk_body(stmt.body, [*scope, stmt.name])

    walk_body(tree.body, [])
    module_hash = _ast_body_hash(tree.body)
    return {"imports": imports, "symbols": symbols, "module_hash": module_hash}


def _diff_python(text_a: str, text_b: str) -> dict[str, Any]:
    sa = _extract_py_structure(text_a)
    sb = _extract_py_structure(text_b)

    imports_added = sorted(sb["imports"] - sa["imports"])
    imports_removed = sorted(sa["imports"] - sb["imports"])

    syms_a = sa["symbols"]
    syms_b = sb["symbols"]
    added = []
    removed = []
    modified = []

    for name in sorted(set(syms_b) - set(syms_a)):
        added.append({"symbol": name, "kind": syms_b[name]["kind"], "signature": syms_b[name]["signature"], "line": syms_b[name]["line"]})
    for name in sorted(set(syms_a) - set(syms_b)):
        removed.append({"symbol": name, "kind": syms_a[name]["kind"], "signature": syms_a[name]["signature"], "line": syms_a[name]["line"]})
    for name in sorted(set(syms_a) & set(syms_b)):
        a, b = syms_a[name], syms_b[name]
        changes = []
        if a["signature"] != b["signature"]:
            changes.append("signature")
        if a["decorators"] != b["decorators"]:
            changes.append("decorators")
        if a["body_hash"] != b["body_hash"]:
            changes.append("body")
        if changes:
            modified.append({
                "symbol": name,
                "kind": b["kind"],
                "changes": changes,
                "before_signature": a["signature"],
                "after_signature": b["signature"],
                "line_before": a["line"],
                "line_after": b["line"],
            })

    semantically_identical = sa["module_hash"] == sb["module_hash"]
    return {
        "ok": True,
        "kind": "python",
        "semantically_identical": semantically_identical,
        "formatting_only": semantically_identical and (text_a != text_b),
        "imports_added": imports_added[:MAX_DIFF_ITEMS],
        "imports_removed": imports_removed[:MAX_DIFF_ITEMS],
        "symbols_added": added[:MAX_DIFF_ITEMS],
        "symbols_removed": removed[:MAX_DIFF_ITEMS],
        "symbols_modified": modified[:MAX_DIFF_ITEMS],
    }


def _diff_mappings(a: Any, b: Any, path: str, out: list[dict[str, Any]]) -> None:
    if len(out) >= MAX_DIFF_ITEMS:
        return
    if type(a) is not type(b):
        out.append({"path": path or "$", "change": "type_changed", "before_type": type(a).__name__, "after_type": type(b).__name__, "before": a, "after": b})
        return
    if isinstance(a, dict):
        keys_a, keys_b = set(a.keys()), set(b.keys())
        for k in sorted(keys_b - keys_a):
            if len(out) >= MAX_DIFF_ITEMS:
                return
            out.append({"path": f"{path}.{k}" if path else str(k), "change": "added", "after": b[k]})
        for k in sorted(keys_a - keys_b):
            if len(out) >= MAX_DIFF_ITEMS:
                return
            out.append({"path": f"{path}.{k}" if path else str(k), "change": "removed", "before": a[k]})
        for k in sorted(keys_a & keys_b):
            _diff_mappings(a[k], b[k], f"{path}.{k}" if path else str(k), out)
        return
    if isinstance(a, list):
        if a == b:
            return
        min_len = min(len(a), len(b))
        for i in range(min_len):
            _diff_mappings(a[i], b[i], f"{path}[{i}]", out)
        for i in range(min_len, len(b)):
            if len(out) >= MAX_DIFF_ITEMS:
                return
            out.append({"path": f"{path}[{i}]", "change": "added", "after": b[i]})
        for i in range(min_len, len(a)):
            if len(out) >= MAX_DIFF_ITEMS:
                return
            out.append({"path": f"{path}[{i}]", "change": "removed", "before": a[i]})
        return
    if a != b:
        out.append({"path": path or "$", "change": "changed", "before": a, "after": b})


def _parse_ini(text: str) -> dict[str, dict[str, str]]:
    cp = configparser.ConfigParser()
    cp.read_string(text)
    return {sec: dict(cp.items(sec)) for sec in cp.sections()}


_GENERIC_SIG_RE = re.compile(
    r"^\s*(?:export\s+)?(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?"
    r"(?:function|fn|func|class|struct|enum|interface|trait|type|def)\s+([A-Za-z_][A-Za-z0-9_]*)(.*)$",
    re.MULTILINE,
)


def _diff_generic(text_a: str, text_b: str) -> dict[str, Any]:
    def extract(src: str) -> dict[str, str]:
        out: dict[str, str] = {}
        for m in _GENERIC_SIG_RE.finditer(src):
            name = m.group(1)
            sig = re.sub(r"\s+", " ", m.group(0).strip())[:160]
            out[name] = sig
        return out

    sa, sb = extract(text_a), extract(text_b)
    added = [{"symbol": k, "signature": sb[k]} for k in sorted(set(sb) - set(sa))]
    removed = [{"symbol": k, "signature": sa[k]} for k in sorted(set(sa) - set(sb))]
    modified = [
        {"symbol": k, "before": sa[k], "after": sb[k]}
        for k in sorted(set(sa) & set(sb))
        if sa[k] != sb[k]
    ]
    lines_a = [ln.strip() for ln in text_a.splitlines() if ln.strip()]
    lines_b = [ln.strip() for ln in text_b.splitlines() if ln.strip()]
    return {
        "ok": True,
        "kind": "generic",
        "semantically_identical": lines_a == lines_b,
        "symbols_added": added[:MAX_DIFF_ITEMS],
        "symbols_removed": removed[:MAX_DIFF_ITEMS],
        "symbols_modified": modified[:MAX_DIFF_ITEMS],
        "nonempty_lines_before": len(lines_a),
        "nonempty_lines_after": len(lines_b),
    }


def diff_semantic(
    *,
    path_a: str = "",
    path_b: str = "",
    text_a: str = "",
    text_b: str = "",
    kind: str = "auto",
    extra_roots: str = "",
) -> dict[str, Any]:
    """Compute a structural/AST-aware diff between two guarded files or two inline strings."""
    try:
        src_a, ext_a = _read_side(path_a, text_a, extra_roots=extra_roots)
        src_b, ext_b = _read_side(path_b, text_b, extra_roots=extra_roots)
    except (OSError, PermissionError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}

    resolved_kind = _infer_kind(kind, ext_a, ext_b, src_a, src_b)
    try:
        if resolved_kind == "python":
            return _diff_python(src_a, src_b)
        if resolved_kind == "json":
            obj_a, obj_b = json.loads(src_a), json.loads(src_b)
            changes: list[dict[str, Any]] = []
            _diff_mappings(obj_a, obj_b, "", changes)
            return {
                "ok": True,
                "kind": "json",
                "semantically_identical": len(changes) == 0,
                "formatting_only": len(changes) == 0 and (src_a != src_b),
                "change_count": len(changes),
                "changes": changes,
            }
        if resolved_kind == "toml":
            obj_a, obj_b = tomllib.loads(src_a), tomllib.loads(src_b)
            changes = []
            _diff_mappings(obj_a, obj_b, "", changes)
            return {
                "ok": True,
                "kind": "toml",
                "semantically_identical": len(changes) == 0,
                "formatting_only": len(changes) == 0 and (src_a != src_b),
                "change_count": len(changes),
                "changes": changes,
            }
        if resolved_kind == "ini":
            obj_a, obj_b = _parse_ini(src_a), _parse_ini(src_b)
            changes = []
            _diff_mappings(obj_a, obj_b, "", changes)
            return {
                "ok": True,
                "kind": "ini",
                "semantically_identical": len(changes) == 0,
                "change_count": len(changes),
                "changes": changes,
            }
        return _diff_generic(src_a, src_b)
    except (SyntaxError, ValueError, tomllib.TOMLDecodeError, configparser.Error) as exc:
        return {"ok": False, "kind": resolved_kind, "error": f"parse error: {exc}"}


__all__ = ["diff_semantic"]
