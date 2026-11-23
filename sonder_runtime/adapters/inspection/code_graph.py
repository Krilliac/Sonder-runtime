"""Guarded, bounded, stdlib-only semantic dependency, call-graph, cycle, and impact analyzer.

Answers high-level architectural questions in a single read-only call:
- ``summary``: module/edge counts, circular dependencies (Tarjan SCCs), top fan-in
  (load-bearing hubs), top fan-out (high-coupling modules), and orphan modules.
- ``impact``: transitive reverse-dependency blast radius for a target file or symbol,
  ranked by hop distance.
- ``cycles``: all strongly connected components of size > 1 (import cycles).
- ``calls``: Python AST call-graph (callers & callees + class inheritance) for a
  symbol or module.

Security invariants:
- Enforces ``file_ops.require_read_access`` on root and every traversed path.
- Refuses symlink/junction targets and skips symlinked descendants and secret/build dirs.
- Hard bounds on file count, file byte size, and wall-clock time; zero subprocesses.
"""
from __future__ import annotations

import ast
from collections import deque
import os
from pathlib import Path
import re
import time
from typing import Any

import sonder_runtime.adapters.filesystem.file_ops as file_ops

DEFAULT_MAX_FILES = 1_500
HARD_MAX_FILES = 5_000
MAX_FILE_BYTES = 1 * 1024 * 1024
DEFAULT_TOP_N = 15
HARD_TOP_N = 50
DEFAULT_TIMEOUT_SECONDS = 12.0

_SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
    "dist", "build", "target", "bin", "obj", ".dart_tool", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", ".idea", ".vscode",
}) | frozenset(file_ops.SENSITIVE_READ_DIRECTORIES)

_CODE_EXTS = frozenset({
    ".py", ".pyw", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx",
    ".rs", ".go", ".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hh",
    ".cs", ".dart", ".java", ".kt",
})

_JS_IMPORT_RE = re.compile(
    r"""(?:import\s+(?:[\s\S]*?\s+from\s+)?|export\s+[\s\S]*?\s+from\s+|require\s*\(\s*)["']([^"']+)["']"""
)
_DART_IMPORT_RE = re.compile(r"""^\s*(?:import|export|part)\s+['"]([^'"]+)['"]""", re.MULTILINE)
_C_INCLUDE_RE = re.compile(r"""^\s*#\s*include\s*"([^"]+)" """, re.MULTILINE | re.VERBOSE)
_RUST_MOD_RE = re.compile(r"""^\s*(?:pub\s+)?(?:mod\s+([A-Za-z_][A-Za-z0-9_]*)\s*;|use\s+(?:crate|super)::([A-Za-z0-9_:]+))""", re.MULTILINE)
_GO_IMPORT_RE = re.compile(r"""^\s*(?:import\s+)?"([^"]+)" """, re.MULTILINE | re.VERBOSE)
_CS_USING_RE = re.compile(r"""^\s*using\s+(?:static\s+)?([A-Za-z_][A-Za-z0-9_.]*)\s*;""", re.MULTILINE)
_JAVA_IMPORT_RE = re.compile(r"""^\s*import\s+(?:static\s+)?([A-Za-z_][A-Za-z0-9_.]*)\s*;""", re.MULTILINE)


def _iter_source_files(root: Path, *, max_files: int, deadline: float) -> tuple[list[Path], bool]:
    found: list[Path] = []
    truncated = False
    if root.is_file():
        if root.suffix.lower() in _CODE_EXTS:
            return [root], False
        return [], False

    stack: list[Path] = [root]
    while stack:
        if time.monotonic() > deadline:
            truncated = True
            break
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError:
            continue
        dirs: list[Path] = []
        for entry in entries:
            if entry.is_symlink():
                continue
            name = entry.name
            if entry.is_dir(follow_symlinks=False):
                if name in _SKIP_DIRS or name.startswith("."):
                    continue
                dirs.append(Path(entry.path))
                continue
            if not entry.is_file(follow_symlinks=False):
                continue
            p = Path(entry.path)
            if p.suffix.lower() not in _CODE_EXTS:
                continue
            try:
                if entry.stat(follow_symlinks=False).st_size > MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            found.append(p)
            if len(found) >= max_files:
                truncated = True
                break
        if truncated:
            break
        for d in reversed(dirs):
            stack.append(d)
    return found, truncated


def _py_module_name(rel_posix: str) -> str:
    no_ext = rel_posix[:-3] if rel_posix.endswith(".py") else rel_posix[:-4]
    parts = no_ext.split("/")
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _resolve_relative_path(base_rel: str, spec: str, rel_set: set[str]) -> str | None:
    base_dir = os.path.dirname(base_rel)
    joined = os.path.normpath(os.path.join(base_dir, spec)).replace("\\", "/")
    if joined in rel_set:
        return joined
    for ext in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".dart", ".h", ".hpp", ".py", ".rs"):
        cand = joined + ext
        if cand in rel_set:
            return cand
    for idx in ("/index.ts", "/index.tsx", "/index.js", "/index.jsx", "/__init__.py", "/mod.rs"):
        cand = joined + idx
        if cand in rel_set:
            return cand
    return None


def _extract_python_info(
    rel: str,
    text: str,
    py_mod_to_rel: dict[str, str],
) -> tuple[set[str], dict[str, set[str]], dict[str, list[str]], dict[str, list[int]]]:
    deps: set[str] = set()
    calls_by_fn: dict[str, set[str]] = {}
    bases_by_cls: dict[str, list[str]] = {}
    defs: dict[str, list[int]] = {}
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return deps, calls_by_fn, bases_by_cls, defs

    cur_mod = _py_module_name(rel)
    cur_pkg_parts = cur_mod.split(".") if rel.endswith("/__init__.py") or rel == "__init__.py" else cur_mod.split(".")[:-1]

    def _link_mod(mod_name: str) -> None:
        if not mod_name:
            return
        if mod_name in py_mod_to_rel:
            deps.add(py_mod_to_rel[mod_name])
            return
        # Try parent module prefixes (e.g. from pkg.mod import symbol)
        parts = mod_name.split(".")
        for i in range(len(parts) - 1, 0, -1):
            prefix = ".".join(parts[:i])
            if prefix in py_mod_to_rel:
                deps.add(py_mod_to_rel[prefix])
                return

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                _link_mod(alias.name)
        elif isinstance(node, ast.ImportFrom):
            level = node.level or 0
            if level > 0:
                up = level - 1
                base_parts = cur_pkg_parts[: len(cur_pkg_parts) - up] if up <= len(cur_pkg_parts) else []
                mod_tail = [p for p in (node.module or "").split(".") if p]
                full_base = ".".join(base_parts + mod_tail)
                _link_mod(full_base)
                for alias in node.names:
                    if alias.name != "*":
                        _link_mod(f"{full_base}.{alias.name}" if full_base else alias.name)
            else:
                base = node.module or ""
                _link_mod(base)
                for alias in node.names:
                    if alias.name != "*":
                        _link_mod(f"{base}.{alias.name}" if base else alias.name)

    def _call_name(fn_node: ast.AST) -> str:
        if isinstance(fn_node, ast.Name):
            return fn_node.id
        if isinstance(fn_node, ast.Attribute):
            return fn_node.attr
        return ""

    class _Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.scope: list[str] = []

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            qname = ".".join([*self.scope, node.name])
            defs.setdefault(qname, []).append(node.lineno)
            bases = []
            for b in node.bases:
                if isinstance(b, ast.Name):
                    bases.append(b.id)
                elif isinstance(b, ast.Attribute):
                    bases.append(b.attr)
            if bases:
                bases_by_cls[qname] = bases
            self.scope.append(node.name)
            self.generic_visit(node)
            self.scope.pop()

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self._visit_fn(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self._visit_fn(node)

        def _visit_fn(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
            qname = ".".join([*self.scope, node.name])
            defs.setdefault(qname, []).append(node.lineno)
            callees = calls_by_fn.setdefault(qname, set())
            self.scope.append(node.name)
            for child in ast.walk(node):
                if isinstance(child, ast.Call):
                    cname = _call_name(child.func)
                    if cname:
                        callees.add(cname)
            self.scope.pop()

    _Visitor().visit(tree)
    deps.discard(rel)
    return deps, calls_by_fn, bases_by_cls, defs


def _extract_other_deps(rel: str, suffix: str, text: str, rel_set: set[str], stem_to_rels: dict[str, list[str]]) -> set[str]:
    deps: set[str] = set()
    if suffix in {".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx"}:
        for m in _JS_IMPORT_RE.finditer(text):
            spec = m.group(1)
            if spec.startswith("."):
                hit = _resolve_relative_path(rel, spec, rel_set)
                if hit:
                    deps.add(hit)
    elif suffix == ".dart":
        for m in _DART_IMPORT_RE.finditer(text):
            spec = m.group(1)
            if spec.startswith("package:"):
                tail = spec.split("/", 1)[1] if "/" in spec else ""
                for cand in (f"lib/{tail}", tail):
                    if cand in rel_set:
                        deps.add(cand)
                        break
            elif not spec.startswith("dart:"):
                hit = _resolve_relative_path(rel, spec, rel_set)
                if hit:
                    deps.add(hit)
    elif suffix in {".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hh"}:
        for m in _C_INCLUDE_RE.finditer(text):
            inc = m.group(1)
            hit = _resolve_relative_path(rel, inc, rel_set)
            if hit:
                deps.add(hit)
            else:
                base = os.path.basename(inc)
                for cand in stem_to_rels.get(base, ()):
                    deps.add(cand)
    elif suffix == ".rs":
        for m in _RUST_MOD_RE.finditer(text):
            mod_name = m.group(1) or (m.group(2).split("::")[0] if m.group(2) else "")
            if mod_name:
                hit = _resolve_relative_path(rel, mod_name, rel_set)
                if hit:
                    deps.add(hit)
    elif suffix == ".go":
        for m in _GO_IMPORT_RE.finditer(text):
            pkg = m.group(1).rsplit("/", 1)[-1]
            for cand in stem_to_rels.get(pkg, ()):
                deps.add(cand)
    elif suffix in {".cs", ".java", ".kt"}:
        pat = _CS_USING_RE if suffix == ".cs" else _JAVA_IMPORT_RE
        for m in pat.finditer(text):
            tail = m.group(1).rsplit(".", 1)[-1]
            for cand in stem_to_rels.get(tail, ()):
                deps.add(cand)
    deps.discard(rel)
    return deps


def _tarjan_scc(nodes: list[str], adj: dict[str, set[str]]) -> list[list[str]]:
    index_counter = [0]
    index: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    cycles: list[list[str]] = []

    for start in nodes:
        if start in index:
            continue
        # Iterative Tarjan to avoid RecursionError on deep graphs.
        call_stack: list[tuple[str, list[str], int]] = [(start, sorted(adj.get(start, ())), 0)]
        index[start] = lowlink[start] = index_counter[0]
        index_counter[0] += 1
        stack.append(start)
        on_stack.add(start)

        while call_stack:
            v, neighbors, pos = call_stack[-1]
            if pos < len(neighbors):
                call_stack[-1] = (v, neighbors, pos + 1)
                w = neighbors[pos]
                if w not in index:
                    index[w] = lowlink[w] = index_counter[0]
                    index_counter[0] += 1
                    stack.append(w)
                    on_stack.add(w)
                    call_stack.append((w, sorted(adj.get(w, ())), 0))
                elif w in on_stack:
                    lowlink[v] = min(lowlink[v], index[w])
            else:
                call_stack.pop()
                if call_stack:
                    parent = call_stack[-1][0]
                    lowlink[parent] = min(lowlink[parent], lowlink[v])
                if lowlink[v] == index[v]:
                    comp: list[str] = []
                    while True:
                        w = stack.pop()
                        on_stack.remove(w)
                        comp.append(w)
                        if w == v:
                            break
                    if len(comp) > 1:
                        cycles.append(sorted(comp))
    return sorted(cycles, key=lambda c: (-len(c), c[0]))


def code_graph(
    root: str = ".",
    *,
    action: str = "summary",
    target: str = "",
    max_files: int = DEFAULT_MAX_FILES,
    top_n: int = DEFAULT_TOP_N,
    max_depth: int = 6,
    extra_roots: str = "",
) -> dict[str, Any]:
    """Analyze repository module dependencies, call graph, cycles, and change blast radius."""
    act = str(action or "summary").strip().lower() or "summary"
    if act not in {"summary", "impact", "cycles", "calls"}:
        return {"ok": False, "error": f"unknown action {act!r} (use summary, impact, cycles, or calls)"}

    resolved_root = file_ops.require_read_access(root or ".", extra_roots=extra_roots)
    if resolved_root.is_symlink():
        raise PermissionError("refusing symlink root")

    cap_files = max(1, min(HARD_MAX_FILES, int(max_files or DEFAULT_MAX_FILES)))
    cap_top = max(1, min(HARD_TOP_N, int(top_n or DEFAULT_TOP_N)))
    depth_cap = max(1, min(20, int(max_depth or 6)))
    deadline = time.monotonic() + DEFAULT_TIMEOUT_SECONDS

    files, truncated = _iter_source_files(resolved_root, max_files=cap_files, deadline=deadline)
    base_dir = resolved_root if resolved_root.is_dir() else resolved_root.parent
    rel_paths = [p.relative_to(base_dir).as_posix() for p in files]
    rel_set = set(rel_paths)

    py_mod_to_rel: dict[str, str] = {}
    stem_to_rels: dict[str, list[str]] = {}
    for rel in rel_paths:
        if rel.endswith((".py", ".pyw")):
            mod = _py_module_name(rel)
            if mod:
                py_mod_to_rel[mod] = rel
        base = os.path.basename(rel)
        stem = os.path.splitext(base)[0]
        stem_to_rels.setdefault(base, []).append(rel)
        stem_to_rels.setdefault(stem, []).append(rel)

    forward: dict[str, set[str]] = {rel: set() for rel in rel_paths}
    reverse: dict[str, set[str]] = {rel: set() for rel in rel_paths}
    all_calls: dict[str, dict[str, set[str]]] = {}
    all_bases: dict[str, dict[str, list[str]]] = {}
    symbol_locs: dict[str, list[dict[str, Any]]] = {}

    for path, rel in zip(files, rel_paths):
        if time.monotonic() > deadline:
            truncated = True
            break
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        suffix = path.suffix.lower()
        if suffix in {".py", ".pyw"}:
            deps, calls_by_fn, bases_by_cls, defs = _extract_python_info(rel, text, py_mod_to_rel)
            if calls_by_fn:
                all_calls[rel] = calls_by_fn
            if bases_by_cls:
                all_bases[rel] = bases_by_cls
            for sym, lines in defs.items():
                short = sym.rsplit(".", 1)[-1]
                for key in {sym, short}:
                    symbol_locs.setdefault(key, []).append({"file": rel, "symbol": sym, "line": lines[0]})
        else:
            deps = _extract_other_deps(rel, suffix, text, rel_set, stem_to_rels)
        forward[rel] = deps
        for dep in deps:
            reverse.setdefault(dep, set()).add(rel)

    edge_count = sum(len(v) for v in forward.values())
    cycles = _tarjan_scc(rel_paths, forward)

    if act == "cycles":
        return {
            "ok": True,
            "action": "cycles",
            "root": str(resolved_root),
            "files_scanned": len(rel_paths),
            "edges": edge_count,
            "cycle_count": len(cycles),
            "cycles": cycles[:cap_top],
            "truncated": truncated,
        }

    if act == "impact":
        needle = str(target or "").strip().replace("\\", "/")
        if not needle:
            return {"ok": False, "error": "action='impact' requires target (relative path, module name, or symbol)"}
        seed_files: list[str] = []
        if needle in rel_set:
            seed_files = [needle]
        elif needle in py_mod_to_rel:
            seed_files = [py_mod_to_rel[needle]]
        else:
            # Match suffix path or symbol definitions
            seed_files = [r for r in rel_paths if r.endswith(needle) or r.endswith("/" + needle)]
            if not seed_files and needle in symbol_locs:
                seed_files = sorted({item["file"] for item in symbol_locs[needle]})
        if not seed_files:
            return {"ok": False, "error": f"target not found in graph: {target!r}"}

        visited: dict[str, int] = {sf: 0 for sf in seed_files}
        q: deque[tuple[str, int]] = deque((sf, 0) for sf in seed_files)
        while q:
            cur, dist = q.popleft()
            if dist >= depth_cap:
                continue
            for importer in sorted(reverse.get(cur, ())):
                if importer not in visited:
                    visited[importer] = dist + 1
                    q.append((importer, dist + 1))

        impacted = [
            {"file": f, "hops": d, "direct_importers": len(reverse.get(f, ()))}
            for f, d in sorted(visited.items(), key=lambda kv: (kv[1], kv[0]))
            if d > 0
        ]
        direct_deps = sorted({dep for sf in seed_files for dep in forward.get(sf, ())})
        return {
            "ok": True,
            "action": "impact",
            "target": target,
            "seed_files": seed_files,
            "direct_dependencies": direct_deps[:cap_top],
            "impacted_count": len(impacted),
            "impacted": impacted[: cap_top * 3],
            "truncated": truncated,
        }

    if act == "calls":
        needle = str(target or "").strip()
        if not needle:
            return {"ok": False, "error": "action='calls' requires target (function/method/class name or file path)"}
        short_needle = needle.rsplit(".", 1)[-1]
        definitions = symbol_locs.get(needle) or symbol_locs.get(short_needle) or []
        callees: dict[str, list[str]] = {}
        callers: list[dict[str, str]] = []
        subclasses: list[dict[str, str]] = []
        for rel, fn_map in all_calls.items():
            for fn_name, called in fn_map.items():
                if fn_name == needle or fn_name.endswith("." + needle) or rel == needle.replace("\\", "/"):
                    callees[f"{rel}::{fn_name}"] = sorted(called)[:40]
                if short_needle in called or needle in called:
                    callers.append({"file": rel, "caller": fn_name})
        for rel, cls_map in all_bases.items():
            for cls_name, bases in cls_map.items():
                if short_needle in bases or needle in bases:
                    subclasses.append({"file": rel, "subclass": cls_name})
        return {
            "ok": True,
            "action": "calls",
            "target": needle,
            "definitions": definitions[:cap_top],
            "caller_count": len(callers),
            "callers": callers[: cap_top * 2],
            "callees": dict(list(callees.items())[:cap_top]),
            "subclasses": subclasses[:cap_top],
            "truncated": truncated,
        }

    # Default: architectural summary
    top_fan_in = sorted(
        ({"file": r, "importers": len(reverse[r]), "imports": len(forward[r])} for r in rel_paths),
        key=lambda item: (-item["importers"], item["file"]),
    )[:cap_top]
    top_fan_out = sorted(
        ({"file": r, "imports": len(forward[r]), "importers": len(reverse[r])} for r in rel_paths),
        key=lambda item: (-item["imports"], item["file"]),
    )[:cap_top]
    orphans = [r for r in rel_paths if not forward[r] and not reverse[r]]

    return {
        "ok": True,
        "action": "summary",
        "root": str(resolved_root),
        "files_scanned": len(rel_paths),
        "edges": edge_count,
        "cycle_count": len(cycles),
        "cycles": cycles[:10],
        "top_hubs_by_importers": [x for x in top_fan_in if x["importers"] > 0],
        "top_coupled_by_imports": [x for x in top_fan_out if x["imports"] > 0],
        "orphan_count": len(orphans),
        "orphan_sample": orphans[:cap_top],
        "truncated": truncated,
    }


__all__ = ["code_graph"]
