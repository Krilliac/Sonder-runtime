"""Guarded, bounded, stdlib-only codebase health and complexity metrics.

Computes in one bounded streaming pass over a repository directory or file:
- Language-aware line counts (``files``, ``code``, ``comment``, ``blank``).
- ``TODO`` / ``FIXME`` / ``HACK`` / ``XXX`` marker inventory with bounded samples.
- Top largest source files.
- Complexity hotspots: Python AST cyclomatic complexity & function length, plus
  branch-density hotspots for C/C++/C#/JS/TS/Dart/Rust/Go/Java files.

Security invariants:
- Enforces ``file_ops.require_read_access``, refuses symlink/junction targets and
  skips symlinked descendants, skips ``.git``/secret/build/vendor directories,
  and bounds file count, bytes scanned, and wall-clock time.
"""
from __future__ import annotations

import ast
import os
from pathlib import Path
import re
import time
from typing import Any

import sonder_runtime.adapters.filesystem.file_ops as file_ops


DEFAULT_MAX_FILES = 2_000
HARD_MAX_FILES = 10_000
DEFAULT_TOP_N = 10
HARD_TOP_N = 50
MAX_FILE_BYTES = 2 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 15.0
HARD_TIMEOUT_SECONDS = 30.0

_SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
    "dist", "build", "target", "bin", "obj", ".dart_tool", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", ".idea", ".vscode",
}) | frozenset(file_ops.SENSITIVE_READ_DIRECTORIES)

_EXT_LANG = {
    ".py": ("python", "#"),
    ".pyw": ("python", "#"),
    ".c": ("c", "//"),
    ".h": ("c/c++", "//"),
    ".cc": ("cpp", "//"),
    ".cpp": ("cpp", "//"),
    ".cxx": ("cpp", "//"),
    ".hpp": ("cpp", "//"),
    ".hh": ("cpp", "//"),
    ".cs": ("csharp", "//"),
    ".js": ("javascript", "//"),
    ".mjs": ("javascript", "//"),
    ".cjs": ("javascript", "//"),
    ".jsx": ("javascript", "//"),
    ".ts": ("typescript", "//"),
    ".tsx": ("typescript", "//"),
    ".dart": ("dart", "//"),
    ".rs": ("rust", "//"),
    ".go": ("go", "//"),
    ".java": ("java", "//"),
    ".kt": ("kotlin", "//"),
    ".kts": ("kotlin", "//"),
    ".swift": ("swift", "//"),
    ".zig": ("zig", "//"),
    ".rb": ("ruby", "#"),
    ".php": ("php", "//"),
    ".lua": ("lua", "--"),
    ".ex": ("elixir", "#"),
    ".exs": ("elixir", "#"),
    ".scala": ("scala", "//"),
    ".hs": ("haskell", "--"),
    ".ml": ("ocaml", ""),
    ".mli": ("ocaml", ""),
    ".jl": ("julia", "#"),
    ".nim": ("nim", "#"),
    ".r": ("r", "#"),
    ".fs": ("fsharp", "//"),
    ".fsx": ("fsharp", "//"),
    ".vb": ("vb", "'"),
    ".groovy": ("groovy", "//"),
    ".gradle": ("groovy", "//"),
    ".pl": ("perl", "#"),
    ".pm": ("perl", "#"),
    ".ps1": ("powershell", "#"),
    ".psm1": ("powershell", "#"),
    ".sh": ("shell", "#"),
    ".bash": ("shell", "#"),
    ".zsh": ("shell", "#"),
    ".bat": ("batch", "REM"),
    ".cmd": ("batch", "REM"),
    ".sql": ("sql", "--"),
    ".proto": ("protobuf", "//"),
    ".graphql": ("graphql", "#"),
    ".gql": ("graphql", "#"),
    ".tf": ("terraform", "#"),
    ".hcl": ("hcl", "#"),
    ".glsl": ("glsl", "//"),
    ".hlsl": ("hlsl", "//"),
    ".wgsl": ("wgsl", "//"),
    ".gd": ("gdscript", "#"),
    ".md": ("markdown", ""),
    ".json": ("json", ""),
    ".toml": ("toml", "#"),
    ".yaml": ("yaml", "#"),
    ".yml": ("yaml", "#"),
    ".xml": ("xml", ""),
    ".ini": ("ini", ";"),
    ".html": ("html", ""),
    ".htm": ("html", ""),
    ".css": ("css", "//"),
    ".scss": ("scss", "//"),
    ".less": ("less", "//"),
    ".vue": ("vue", "//"),
    ".svelte": ("svelte", "//"),
}

_MARKER_RE = re.compile(r"\b(TODO|FIXME|HACK|XXX)\b[:\s-]*(.*)", re.IGNORECASE)
_BRACE_BRANCH_RE = re.compile(
    r"\b(?:if|else\s+if|for|while|case|catch|switch)\b|\&\&|\|\||\?"
)
_PY_BRANCH_NODES = (
    ast.If, ast.For, ast.AsyncFor, ast.While, ast.ExceptHandler,
    ast.With, ast.AsyncWith, ast.IfExp, ast.comprehension, ast.Match,
)


class CodeMetricsError(ValueError):
    """Stable rejection from bounded code metrics inspection."""


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def _resolve_target(
    path: str,
    *,
    extra_roots: str = "",
    bypass: bool = False,
    developer_authorized: bool = False,
) -> Path:
    requested = file_ops._requested_path(str(path or "."))
    if file_ops._is_reparse_point(requested):
        raise CodeMetricsError("metrics target must not be a symlink or junction")
    try:
        target = file_ops.require_read_access(
            str(path or "."),
            extra_roots=extra_roots,
            bypass=bypass,
            developer_authorized=developer_authorized,
        )
    except (OSError, PermissionError, TypeError, ValueError) as exc:
        raise CodeMetricsError("metrics path rejected: %s" % exc) from exc
    if not target.exists():
        raise CodeMetricsError("metrics target does not exist")
    return target


def _python_hotspots(source: str, rel_path: str) -> list[dict[str, Any]]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    hotspots: list[dict[str, Any]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        complexity = 1
        for child in ast.walk(node):
            if isinstance(child, _PY_BRANCH_NODES):
                complexity += 1
            elif isinstance(child, ast.BoolOp):
                complexity += max(0, len(child.values) - 1)
        start = getattr(node, "lineno", 1)
        end = getattr(node, "end_lineno", start) or start
        length = max(1, end - start + 1)
        if complexity >= 8 or length >= 60:
            hotspots.append({
                "file": rel_path,
                "symbol": node.name,
                "line": start,
                "lines": length,
                "complexity": complexity,
                "kind": "python_ast",
            })
    return hotspots


def _analyze_file(path: Path, rel_path: str, lang: str, comment_prefix: str) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    raw = path.read_text(encoding="utf-8", errors="replace")
    lines = raw.splitlines()
    code_lines = 0
    comment_lines = 0
    blank_lines = 0
    markers: list[dict[str, Any]] = []
    branch_hits = 0

    for lineno, line in enumerate(lines, 1):
        stripped = line.strip()
        if not stripped:
            blank_lines += 1
            continue
        if comment_prefix and stripped.startswith(comment_prefix):
            comment_lines += 1
        else:
            code_lines += 1
            if comment_prefix == "//":
                branch_hits += len(_BRACE_BRANCH_RE.findall(stripped))
        m = _MARKER_RE.search(line)
        if m:
            markers.append({
                "tag": m.group(1).upper(),
                "file": rel_path,
                "line": lineno,
                "text": m.group(2).strip()[:120],
            })

    hotspots: list[dict[str, Any]] = []
    if lang == "python":
        hotspots = _python_hotspots(raw, rel_path)
    elif comment_prefix == "//" and code_lines >= 40 and branch_hits >= 25:
        hotspots.append({
            "file": rel_path,
            "symbol": Path(rel_path).name,
            "line": 1,
            "lines": code_lines,
            "complexity": branch_hits,
            "kind": "branch_density",
        })

    stats = {
        "language": lang,
        "code": code_lines,
        "comment": comment_lines,
        "blank": blank_lines,
        "total": len(lines),
    }
    return stats, markers, hotspots


def analyze_code_metrics(
    path: str = ".",
    *,
    max_files: int = DEFAULT_MAX_FILES,
    top_n: int = DEFAULT_TOP_N,
    extra_roots: str = "",
    bypass: bool = False,
    developer_authorized: bool = False,
) -> dict[str, Any]:
    """Compute bounded LOC, TODO/FIXME inventory, largest files, and complexity hotspots."""
    target = _resolve_target(
        path,
        extra_roots=extra_roots,
        bypass=bypass,
        developer_authorized=developer_authorized,
    )
    file_cap = _bounded_int(max_files, DEFAULT_MAX_FILES, 1, HARD_MAX_FILES)
    top_cap = _bounded_int(top_n, DEFAULT_TOP_N, 1, HARD_TOP_N)
    deadline = time.monotonic() + DEFAULT_TIMEOUT_SECONDS

    languages: dict[str, dict[str, int]] = {}
    marker_counts = {"TODO": 0, "FIXME": 0, "HACK": 0, "XXX": 0}
    marker_samples: list[dict[str, Any]] = []
    largest_files: list[dict[str, Any]] = []
    hotspots: list[dict[str, Any]] = []
    files_scanned = 0
    files_skipped = 0
    truncated = False
    truncation_reasons: list[str] = []

    def _visit_file(file_path: Path, base: Path) -> None:
        nonlocal files_scanned, files_skipped
        info = _EXT_LANG.get(file_path.suffix.lower())
        if not info:
            return
        try:
            size = file_path.stat().st_size
        except OSError:
            files_skipped += 1
            return
        if size > MAX_FILE_BYTES:
            files_skipped += 1
            return
        rel = str(file_path.relative_to(base)) if file_path != base else file_path.name
        lang, prefix = info
        try:
            stats, markers, file_hotspots = _analyze_file(file_path, rel, lang, prefix)
        except OSError:
            files_skipped += 1
            return
        files_scanned += 1
        bucket = languages.setdefault(lang, {"files": 0, "code": 0, "comment": 0, "blank": 0})
        bucket["files"] += 1
        bucket["code"] += stats["code"]
        bucket["comment"] += stats["comment"]
        bucket["blank"] += stats["blank"]
        largest_files.append({
            "file": rel,
            "language": lang,
            "code_lines": stats["code"],
            "total_lines": stats["total"],
        })
        for mk in markers:
            tag = mk["tag"]
            marker_counts[tag] = marker_counts.get(tag, 0) + 1
            if len(marker_samples) < top_cap * 3:
                marker_samples.append(mk)
        hotspots.extend(file_hotspots)

    if target.is_file():
        _visit_file(target, target.parent)
    else:
        for dirpath, dirnames, filenames in os.walk(target, followlinks=False):
            if time.monotonic() >= deadline:
                truncated = True
                truncation_reasons.append("timeout")
                break
            dirnames[:] = sorted(
                d for d in dirnames
                if d.lower() not in _SKIP_DIRS
                and not file_ops._is_reparse_point(Path(dirpath) / d)
            )
            for fname in sorted(filenames):
                if files_scanned >= file_cap:
                    truncated = True
                    truncation_reasons.append("max_files")
                    break
                if time.monotonic() >= deadline:
                    truncated = True
                    truncation_reasons.append("timeout")
                    break
                fpath = Path(dirpath) / fname
                if file_ops._is_reparse_point(fpath):
                    files_skipped += 1
                    continue
                _visit_file(fpath, target)
            if truncated:
                break

    largest_files.sort(key=lambda item: (-item["code_lines"], item["file"]))
    hotspots.sort(key=lambda item: (-item["complexity"], -item["lines"], item["file"]))
    totals = {
        "files": files_scanned,
        "code": sum(v["code"] for v in languages.values()),
        "comment": sum(v["comment"] for v in languages.values()),
        "blank": sum(v["blank"] for v in languages.values()),
    }
    return {
        "ok": True,
        "root": str(target),
        "totals": totals,
        "files_skipped": files_skipped,
        "languages": dict(sorted(languages.items(), key=lambda kv: (-kv[1]["code"], kv[0]))),
        "marker_counts": marker_counts,
        "marker_samples": marker_samples[: top_cap * 2],
        "largest_files": largest_files[:top_cap],
        "complexity_hotspots": hotspots[:top_cap],
        "truncated": truncated,
        "truncation_reasons": sorted(set(truncation_reasons)),
    }


def format_result(data: dict[str, Any]) -> str:
    totals = data.get("totals") or {}
    lines = [
        f"code metrics: {data.get('root', '')}",
        (
            f"  totals: files={totals.get('files', 0)} code={totals.get('code', 0)} "
            f"comment={totals.get('comment', 0)} blank={totals.get('blank', 0)}"
        ),
    ]
    langs = data.get("languages") or {}
    if langs:
        lines.append("  languages:")
        for lang, st in list(langs.items())[:12]:
            lines.append(
                f"    {lang:<12} files={st['files']:<4} code={st['code']:<7} "
                f"comment={st['comment']:<6} blank={st['blank']}"
            )
    mc = data.get("marker_counts") or {}
    if any(mc.values()):
        lines.append(
            "  markers: " + " ".join(f"{k}={v}" for k, v in mc.items() if v)
        )
    hotspots = data.get("complexity_hotspots") or []
    if hotspots:
        lines.append("  complexity hotspots:")
        for h in hotspots[:10]:
            lines.append(
                f"    {h['file']}:{h['line']} {h['symbol']} "
                f"(complexity={h['complexity']}, lines={h['lines']})"
            )
    largest = data.get("largest_files") or []
    if largest:
        lines.append("  largest files:")
        for lf in largest[:8]:
            lines.append(f"    {lf['file']} ({lf['language']}, {lf['code_lines']} code lines)")
    if data.get("truncated"):
        lines.append(f"  truncated: true ({', '.join(data.get('truncation_reasons') or [])})")
    return "\n".join(lines)


__all__ = [
    "CodeMetricsError",
    "analyze_code_metrics",
    "format_result",
]
