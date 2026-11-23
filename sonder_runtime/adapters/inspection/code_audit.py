"""Guarded adapter for ``code_audit`` (/audit).

Audits inline source code or a guarded repository file for security
vulnerabilities and classic bug footguns (Python AST + generic multi-language rules).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import sonder_runtime.adapters.filesystem.file_ops as file_ops
from sonder_runtime.domain.code_audit import audit_source

MAX_CODE_CHARS = 180_000
MAX_FILE_BYTES = 1 * 1024 * 1024


def _normalize_extra_roots(extra_roots: str | list[str] | tuple[str, ...] | None) -> str:
    if not extra_roots:
        return ""
    if isinstance(extra_roots, str):
        return extra_roots
    return os.pathsep.join(str(r) for r in extra_roots if str(r).strip())


def code_audit(
    code: str = "",
    *,
    path: str = "",
    language: str = "",
    min_severity: str = "low",
    max_findings: int = 60,
    extra_roots: str | list[str] | None = None,
) -> dict[str, Any]:
    """Audit inline code or a guarded file for security vulnerabilities and bug footguns."""
    src = str(code or "")
    filename = ""

    if path and not src:
        raw_path = str(path)
        p = Path(raw_path)
        resolved = file_ops.require_read_access(raw_path, extra_roots=_normalize_extra_roots(extra_roots))
        if p.is_symlink() or resolved.is_symlink():
            return {"ok": False, "error": "refusing symlink"}
        if not resolved.is_file():
            return {"ok": False, "error": f"file does not exist: {path}"}
        if resolved.stat().st_size > MAX_FILE_BYTES:
            return {"ok": False, "error": f"file exceeds {MAX_FILE_BYTES} byte limit"}
        src = resolved.read_text(encoding="utf-8", errors="replace")
        filename = str(resolved)
    elif path:
        filename = str(path)

    if not src.strip():
        return {"ok": False, "error": "provide non-empty `code` or `path` to audit"}

    truncated = False
    if len(src) > MAX_CODE_CHARS:
        src = src[:MAX_CODE_CHARS]
        truncated = True

    result = audit_source(
        src,
        filename=filename,
        language=language,
        min_severity=min_severity,
        max_findings=max(1, min(120, int(max_findings))),
    )
    result["truncated"] = truncated
    return result
