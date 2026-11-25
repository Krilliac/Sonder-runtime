"""Pure domain policy and validation for Sonder's persistent scratchpad.

Free-form, restart-persistent Markdown working notes stored per caller owner.
This module performs zero I/O so policy and validation rules stay testable and
independent of filesystem and transport adapters.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_TAG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,31}$")
_OWNER_SAFE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,47}$")

_WINDOWS_RESERVED = frozenset({
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
})

VALID_WRITE_MODES = frozenset({"replace", "append"})


@dataclass(frozen=True)
class ScratchpadPolicy:
    """Hard resource bounds enforced fail-closed by the scratchpad store."""

    max_note_bytes: int = 65_536          # 64 KiB per note
    max_notes_per_owner: int = 64         # 64 notes per owner
    max_total_bytes_per_owner: int = 1_048_576  # 1 MiB total per owner
    max_name_chars: int = 64
    max_tags: int = 16
    max_tag_chars: int = 32
    max_read_lines: int = 400
    max_search_results: int = 20
    preview_chars: int = 160


DEFAULT_POLICY = ScratchpadPolicy()


def validate_note_name(name: str, policy: ScratchpadPolicy = DEFAULT_POLICY) -> str:
    """Validate and return a clean scratchpad note slug.

    Accepts an optional trailing ``.md`` suffix (stripped so ``plan`` and
    ``plan.md`` address the same note). Rejects empty names, traversal
    segments, hidden dotfiles, path separators, NUL bytes, and Windows
    reserved device names.
    """
    if not isinstance(name, str):
        raise TypeError("scratchpad note name must be a string")
    raw = name.strip()
    if raw.lower().endswith(".md") and len(raw) > 3:
        raw = raw[:-3]
    if not raw or len(raw) > policy.max_name_chars:
        raise ValueError(f"scratchpad note name must be 1..{policy.max_name_chars} characters")
    if "\x00" in raw or "/" in raw or "\\" in raw or ".." in raw:
        raise ValueError("scratchpad note name must not contain path separators or traversal")
    if not _NAME_RE.match(raw):
        raise ValueError(
            "scratchpad note name must start with an alphanumeric character and use only "
            "ASCII letters, digits, '.', '_', or '-'"
        )
    stem = raw.split(".", 1)[0].upper()
    if stem in _WINDOWS_RESERVED or raw.startswith("_"):
        raise ValueError(f"scratchpad note name {raw!r} is reserved")
    return raw


def normalize_owner(owner: str | None) -> str:
    """Map an arbitrary caller principal/owner string into a collision-free folder slug."""
    raw = str(owner or "").strip()
    if not raw or raw.lower() == "local":
        return "local"
    if _OWNER_SAFE_RE.match(raw) and ".." not in raw and raw.split(".", 1)[0].upper() not in _WINDOWS_RESERVED:
        return raw
    prefix = re.sub(r"[^A-Za-z0-9_-]+", "_", raw).strip("_")[:24] or "owner"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}-{digest}"


def normalize_tags(
    tags: list[str] | tuple[str, ...] | None,
    policy: ScratchpadPolicy = DEFAULT_POLICY,
) -> tuple[str, ...]:
    """Validate, lowercase, and deduplicate optional note tags."""
    if tags is None:
        return ()
    if isinstance(tags, (str, bytes)) or not isinstance(tags, (list, tuple)):
        raise TypeError("tags must be a list of short strings")
    out: list[str] = []
    seen: set[str] = set()
    for item in tags:
        if not isinstance(item, str):
            raise TypeError("each tag must be a string")
        cleaned = item.strip().lower()
        if not cleaned:
            continue
        if len(cleaned) > policy.max_tag_chars or not _TAG_RE.match(cleaned):
            raise ValueError(
                f"invalid tag {item!r}: use 1..{policy.max_tag_chars} lowercase ASCII letters, digits, '.', '_', or '-'"
            )
        if cleaned not in seen:
            seen.add(cleaned)
            out.append(cleaned)
            if len(out) > policy.max_tags:
                raise ValueError(f"at most {policy.max_tags} tags allowed per scratchpad note")
    return tuple(out)


def validate_positive_int(name: str, value: object, *, minimum: int = 1, maximum: int) -> int:
    """Validate bounded integer arguments while rejecting booleans."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer between {minimum} and {maximum}")
    if value < minimum or value > maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def preview_line(content: str, limit: int = 160) -> str:
    """Return the first non-empty line trimmed for list previews."""
    for line in content.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:limit]
    return ""


__all__ = [
    "VALID_WRITE_MODES",
    "ScratchpadPolicy",
    "normalize_owner",
    "normalize_tags",
    "preview_line",
    "validate_note_name",
    "validate_positive_int",
]
