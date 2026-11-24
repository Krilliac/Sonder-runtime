"""Guarded adapter for ``data_transform`` (/transform).

Deterministic structured data conversion (JSON/CSV/TSV/INI/TOML/Markdown),
statistical column profiling, safe dot-path/filter/sort/group querying,
Base64/URL/Hex/Unicode encoding, timestamp conversion, and 5-field cron math.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import sonder_runtime.adapters.filesystem.file_ops as file_ops
from sonder_runtime.domain.data_transform import run_data_transform

MAX_DATA_CHARS = 200_000
MAX_FILE_BYTES = 2 * 1024 * 1024


def _normalize_extra_roots(extra_roots: str | list[str] | tuple[str, ...] | None) -> str:
    if not extra_roots:
        return ""
    if isinstance(extra_roots, str):
        return extra_roots
    return os.pathsep.join(str(r) for r in extra_roots if str(r).strip())


def data_transform(
    action: str = "convert",
    data: str = "",
    *,
    path: str = "",
    input_format: str = "auto",
    output_format: str = "json",
    expression: str = "",
    encoding: str = "base64",
    limit: int = 50,
    extra_roots: str | list[str] | None = None,
) -> dict[str, Any]:
    """Convert, profile, query, encode/decode structured data, timestamps, or cron schedules."""
    payload = str(data or "")
    fmt = input_format

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
        if fmt == "auto":
            ext = resolved.suffix.lower().lstrip(".")
            if ext in ("json", "jsonl", "csv", "tsv", "toml", "ini"):
                fmt = ext

    if len(payload) > MAX_DATA_CHARS:
        payload = payload[:MAX_DATA_CHARS]

    return run_data_transform(
        action=action,
        data=payload,
        input_format=fmt,
        output_format=output_format,
        expression=expression,
        encoding=encoding,
        limit=max(1, min(200, int(limit))),
    )
