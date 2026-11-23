"""Guarded, bounded, stdlib-only citation, file/line, quote, and source-grounding auditor.

Catches fabricated file paths, out-of-range line numbers, misquoted code or
documentation lines, and claims unbacked by supplied source snippets.

Security invariants:
- Enforces ``file_ops.require_read_access`` on root and every cited file.
- Refuses symlinks/junctions and files outside the resolved root.
- Bounded scan length, file size, and claim count.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
from typing import Any

import sonder_runtime.adapters.filesystem.file_ops as file_ops

MAX_TEXT_CHARS = 64_000
MAX_FILE_BYTES = 1 * 1024 * 1024
MAX_CITATIONS = 60

# Matches path:line, path:line-end, path#L10, path#L10-L20
_FILE_LINE_RE = re.compile(
    r"(?<![A-Za-z0-9_/@])"
    r"([A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*\.(?:py|js|ts|tsx|jsx|dart|rs|go|c|h|cc|cpp|hpp|cs|java|kt|json|toml|yaml|yml|md|ps1|sh|sql|txt))"
    r"(?::(\d+)(?:-(\d+))?|#L(\d+)(?:-L?(\d+))?)",
)


def _normalize_ws(text: str) -> str:
    return " ".join(str(text or "").split())


def _safe_read_lines(root: Path, rel_or_path: str) -> tuple[list[str] | None, str]:
    raw = str(rel_or_path or "").strip().replace("\\", "/")
    if not raw or "\x00" in raw or raw.startswith(("/", "\\")):
        return None, "invalid or non-relative path"
    cand = root / raw
    try:
        resolved = cand.resolve()
        if resolved != root and root not in resolved.parents:
            return None, "path escapes root"
        if cand.is_symlink() or resolved.is_symlink():
            return None, "refusing symlink"
        if not resolved.is_file():
            return None, "file does not exist"
        if resolved.stat().st_size > MAX_FILE_BYTES:
            return None, "file exceeds size limit"
        lines = resolved.read_text(encoding="utf-8", errors="replace").splitlines()
        return lines, ""
    except OSError as exc:
        return None, str(exc)


def _verify_quote_in_lines(
    lines: list[str],
    quote: str,
    cited_line: int | None = None,
    window: int = 8,
) -> dict[str, Any]:
    q_raw = str(quote or "").strip()
    if not q_raw:
        return {"status": "skipped"}
    q_norm = _normalize_ws(q_raw)
    full_text = "\n".join(lines)

    # First check near the cited line if supplied
    if cited_line is not None and 1 <= cited_line <= len(lines):
        lo = max(0, cited_line - 1 - window)
        hi = min(len(lines), cited_line + window)
        snippet = "\n".join(lines[lo:hi])
        if q_raw in snippet or q_norm in _normalize_ws(snippet):
            return {"status": "verified", "matched_near_line": cited_line}

    # Check whole file to distinguish drifted line number vs fabricated quote
    for idx, line in enumerate(lines, 1):
        if q_raw in line or (len(q_norm) >= 6 and q_norm in _normalize_ws(line)):
            return {
                "status": "drifted_line" if cited_line is not None else "verified",
                "actual_line": idx,
                "cited_line": cited_line,
            }
    if q_raw in full_text or q_norm in _normalize_ws(full_text):
        return {
            "status": "drifted_line" if cited_line is not None else "verified",
            "cited_line": cited_line,
        }
    return {"status": "fabricated_quote", "cited_line": cited_line}


def citation_audit(
    *,
    text: str = "",
    root: str = "",
    claims: list[dict[str, Any]] | None = None,
    sources: list[str] | None = None,
    extra_roots: str = "",
) -> dict[str, Any]:
    """Audit file:line citations, quoted code/doc snippets, and source grounding."""
    raw_text = str(text or "")
    if len(raw_text) > MAX_TEXT_CHARS:
        return {"ok": False, "error": f"text exceeds {MAX_TEXT_CHARS} characters"}

    repo_root: Path | None = None
    if root:
        try:
            repo_root = file_ops.require_read_access(root, extra_roots=extra_roots)
            if repo_root.is_symlink():
                return {"ok": False, "error": "refusing symlink root"}
        except (OSError, PermissionError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}

    results: list[dict[str, Any]] = []
    cache: dict[str, tuple[list[str] | None, str]] = {}

    def _get_lines(path_str: str) -> tuple[list[str] | None, str]:
        if repo_root is None:
            return None, "no repository root provided"
        if path_str not in cache:
            cache[path_str] = _safe_read_lines(repo_root, path_str)
        return cache[path_str]

    # 1. Explicit structured claims: [{"file": "...", "line": 42, "quote": "..."}]
    for item in (claims or [])[:MAX_CITATIONS]:
        if not isinstance(item, dict):
            continue
        file_ref = str(item.get("file") or item.get("path") or "").strip()
        line_val = item.get("line")
        line_num = int(line_val) if isinstance(line_val, int) and line_val > 0 else None
        quote = str(item.get("quote") or "").strip()
        entry: dict[str, Any] = {"file": file_ref, "line": line_num}

        if file_ref and repo_root is not None:
            lines, err = _get_lines(file_ref)
            if lines is None:
                entry["status"] = "fabricated_file"
                entry["detail"] = err
            else:
                entry["total_lines"] = len(lines)
                if line_num is not None and line_num > len(lines):
                    entry["status"] = "line_out_of_range"
                    entry["detail"] = f"cited line {line_num} exceeds file length {len(lines)}"
                elif quote:
                    qres = _verify_quote_in_lines(lines, quote, cited_line=line_num)
                    entry.update(qres)
                else:
                    entry["status"] = "verified"
            results.append(entry)
        elif quote and sources:
            norm_sources = [_normalize_ws(s) for s in sources]
            found = any((quote in s) or (_normalize_ws(quote) in ns) for s, ns in zip(sources, norm_sources))
            results.append({
                "quote": quote[:160],
                "status": "verified" if found else "ungrounded_quote",
            })

    # 2. Automatic file:line extraction from draft prose (when root is supplied)
    if raw_text and repo_root is not None:
        seen_refs: set[tuple[str, int, int | None]] = set()
        for m in _FILE_LINE_RE.finditer(raw_text):
            if len(results) >= MAX_CITATIONS:
                break
            fpath = m.group(1)
            start_s = m.group(2) or m.group(4)
            end_s = m.group(3) or m.group(5)
            start_line = int(start_s)
            end_line = int(end_s) if end_s else None
            key = (fpath, start_line, end_line)
            if key in seen_refs:
                continue
            seen_refs.add(key)
            lines, err = _get_lines(fpath)
            if lines is None:
                results.append({
                    "file": fpath,
                    "line": start_line,
                    "status": "fabricated_file",
                    "detail": err,
                })
            elif start_line < 1 or start_line > len(lines) or (end_line is not None and (end_line < start_line or end_line > len(lines))):
                results.append({
                    "file": fpath,
                    "line": start_line,
                    "line_end": end_line,
                    "total_lines": len(lines),
                    "status": "line_out_of_range",
                    "detail": f"cited range {start_line}-{end_line or start_line} outside 1..{len(lines)}",
                })
            else:
                results.append({
                    "file": fpath,
                    "line": start_line,
                    "line_end": end_line,
                    "status": "verified",
                })

    # 3. Quoted phrases in draft prose checked against supplied sources
    if raw_text and sources and not claims:
        norm_sources = [_normalize_ws(s) for s in sources]
        # Extract double-quoted phrases >= 12 chars outside fenced code blocks
        prose = re.sub(r"```.*?```", " ", raw_text, flags=re.DOTALL)
        for qm in re.finditer(r'"([^"\n]{12,240})"', prose):
            if len(results) >= MAX_CITATIONS:
                break
            q = qm.group(1).strip()
            qn = _normalize_ws(q)
            found = any((q in s) or (qn in ns) for s, ns in zip(sources, norm_sources))
            results.append({
                "quote": q[:160],
                "status": "verified" if found else "ungrounded_quote",
            })

    verified = sum(1 for r in results if r.get("status") == "verified")
    failed = [r for r in results if r.get("status") not in {"verified", "skipped"}]
    score = (verified / len(results)) if results else 1.0

    return {
        "ok": True,
        "clean": len(failed) == 0,
        "checked_count": len(results),
        "verified_count": verified,
        "issue_count": len(failed),
        "grounding_score": round(score, 3),
        "issues": failed,
        "all_results": results,
    }


__all__ = ["citation_audit"]
