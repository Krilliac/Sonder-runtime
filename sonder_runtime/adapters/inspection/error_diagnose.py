"""Guarded adapter for ``error_diagnose`` (/diagnose).

Parses multi-language tracebacks and compiler errors and optionally enriches
repository frames with exact surrounding source lines via ``file_ops``.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import sonder_runtime.adapters.filesystem.file_ops as file_ops
from sonder_runtime.domain.error_diagnosis import parse_error_report

MAX_ERROR_CHARS = 64_000
MAX_FILE_BYTES = 1 * 1024 * 1024


def _normalize_extra_roots(extra_roots: str | list[str] | tuple[str, ...] | None) -> str:
    if not extra_roots:
        return ""
    if isinstance(extra_roots, str):
        return extra_roots
    return os.pathsep.join(str(r) for r in extra_roots if str(r).strip())


def _read_context_snippet(
    file_ref: str,
    lineno: int,
    *,
    root_path: Path | None,
    context_lines: int,
    extra_roots_str: str,
) -> list[str] | None:
    raw = str(file_ref or "").strip()
    if not raw or "\x00" in raw:
        return None
    candidates: list[Path] = []
    p = Path(raw)
    if p.is_absolute():
        candidates.append(p)
    elif root_path is not None:
        candidates.append(root_path / raw)
    else:
        candidates.append(p)

    for cand in candidates:
        try:
            resolved = file_ops.require_read_access(str(cand), extra_roots=extra_roots_str)
            if root_path is not None and resolved != root_path and root_path not in resolved.parents:
                continue
            if cand.is_symlink() or resolved.is_symlink() or not resolved.is_file():
                continue
            if resolved.stat().st_size > MAX_FILE_BYTES:
                continue
            lines = resolved.read_text(encoding="utf-8", errors="replace").splitlines()
            if not (1 <= lineno <= len(lines)):
                return None
            lo = max(1, lineno - context_lines)
            hi = min(len(lines), lineno + context_lines)
            out: list[str] = []
            for idx in range(lo, hi + 1):
                marker = ">>>" if idx == lineno else "   "
                out.append(f"{marker} {idx:5d} | {lines[idx - 1]}")
            return out
        except Exception:
            continue
    return None


def error_diagnose(
    error_text: str,
    *,
    root: str = "",
    max_frames: int = 8,
    context_lines: int = 3,
    extra_roots: str | list[str] | None = None,
) -> dict[str, Any]:
    """Parse stacktrace/compiler diagnostics, resolve repo source context, and classify root cause."""
    text = str(error_text or "")
    if len(text) > MAX_ERROR_CHARS:
        text = text[-MAX_ERROR_CHARS:]
    report = parse_error_report(text, max_frames=max(1, min(24, int(max_frames))))
    if not report.get("ok"):
        return report

    extra_roots_str = _normalize_extra_roots(extra_roots)
    root_path: Path | None = None
    if root:
        resolved_root = file_ops.require_read_access(str(root), extra_roots=extra_roots_str)
        if resolved_root.is_dir():
            root_path = resolved_root

    ctx = max(0, min(10, int(context_lines)))
    resolved_count = 0
    if ctx > 0 and report.get("frames"):
        for frame in report["frames"]:
            snippet = _read_context_snippet(
                frame["file"],
                int(frame["line"]),
                root_path=root_path,
                context_lines=ctx,
                extra_roots_str=extra_roots_str,
            )
            if snippet is not None:
                frame["source_context"] = snippet
                resolved_count += 1
        primary = report.get("primary_frame")
        if isinstance(primary, dict):
            for frame in report["frames"]:
                if frame["file"] == primary["file"] and frame["line"] == primary["line"] and "source_context" in frame:
                    primary["source_context"] = frame["source_context"]
                    break

    report["resolved_repo_frames"] = resolved_count
    return report
