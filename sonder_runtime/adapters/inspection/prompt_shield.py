"""Guarded adapter for ``prompt_shield`` (/shield).

Scans inline text or a guarded repository file for prompt-injection payloads,
Trojan-Source Unicode bidi/tag spoofing, and accidental secret/credential leaks.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import sonder_runtime.adapters.filesystem.file_ops as file_ops
from sonder_runtime.domain.prompt_defense import scan_untrusted_text

MAX_TEXT_CHARS = 120_000
MAX_FILE_BYTES = 1 * 1024 * 1024


def _normalize_extra_roots(extra_roots: str | list[str] | tuple[str, ...] | None) -> str:
    if not extra_roots:
        return ""
    if isinstance(extra_roots, str):
        return extra_roots
    return os.pathsep.join(str(r) for r in extra_roots if str(r).strip())


def prompt_shield(
    text: str = "",
    *,
    path: str = "",
    redact_secrets: bool = True,
    include_sanitized: bool = True,
    max_findings: int = 40,
    extra_roots: str | list[str] | None = None,
) -> dict[str, Any]:
    """Scan inline text or a guarded file for prompt injection, Trojan-Source Unicode, and secrets."""
    source_label = "inline"
    payload = str(text or "")

    if path and not payload:
        raw_path = str(path)
        p = Path(raw_path)
        resolved = file_ops.require_read_access(raw_path, extra_roots=_normalize_extra_roots(extra_roots))
        if p.is_symlink() or resolved.is_symlink():
            return {"ok": False, "error": "refusing symlink"}
        if not resolved.is_file():
            return {"ok": False, "error": f"file does not exist: {path}"}
        if resolved.stat().st_size > MAX_FILE_BYTES:
            return {"ok": False, "error": f"file exceeds {MAX_FILE_BYTES} byte limit"}
        payload = resolved.read_text(encoding="utf-8", errors="replace")
        source_label = str(resolved)

    if not payload.strip():
        return {"ok": False, "error": "provide non-empty `text` or `path` to scan"}

    truncated = False
    if len(payload) > MAX_TEXT_CHARS:
        payload = payload[:MAX_TEXT_CHARS]
        truncated = True

    result = scan_untrusted_text(
        payload,
        redact_secrets=bool(redact_secrets),
        max_findings=max(1, min(100, int(max_findings))),
    )
    result["source"] = source_label
    result["truncated"] = truncated
    if not include_sanitized:
        result.pop("sanitized_text", None)
    elif len(str(result.get("sanitized_text") or "")) > 8000:
        result["sanitized_text"] = str(result["sanitized_text"])[:8000] + "\n... (sanitized preview truncated)"
    return result
