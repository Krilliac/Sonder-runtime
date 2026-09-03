"""Pure argument normalization for the context-pack tool.

The public tool takes a JSON array of paths, bounded integers and a byte
budget; these helpers validate the shapes and clip a UTF-8 body without
splitting a codepoint, and never resolve or read a path. Moved from
``server.py`` in the WP1 Three-Hundred-Ninth Slice with its behaviour
byte-for-byte intact.
"""
from __future__ import annotations

import json


def pack_paths(paths_json) -> list[str]:
    """Normalize the public JSON/list shape without resolving any paths."""
    try:
        value = json.loads(paths_json) if isinstance(paths_json, str) else paths_json
    except (TypeError, ValueError) as exc:
        raise ValueError("paths_json must be a JSON array of file paths") from exc
    if not isinstance(value, list):
        raise ValueError("paths_json must be a JSON array of file paths")
    if not value:
        raise ValueError("paths_json must contain at least one file path")
    paths = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise ValueError("paths_json item %d must be a non-empty string" % (index + 1))
        if "\x00" in item:
            raise ValueError("paths_json item %d contains a NUL byte" % (index + 1))
        paths.append(item.strip())
    return paths


def pack_int(value, default: int, ceiling: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(1, min(ceiling, number))


def pack_utf8_prefix(text: str, max_bytes: int) -> tuple[str, int, bool]:
    raw = str(text or "").encode("utf-8")
    if len(raw) <= max_bytes:
        return str(text or ""), len(raw), False
    prefix = raw[:max_bytes]
    # Do not emit half of a multibyte codepoint. The body can therefore be a
    # few bytes below the cap, but never above it.
    decoded = prefix.decode("utf-8", errors="ignore")
    return decoded, len(decoded.encode("utf-8")), True
