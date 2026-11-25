"""Restart-persistent, owner-scoped Markdown scratchpad store.

Each owner gets a private directory under ``<sonder_home>/scratchpad/<owner>/``
containing human-editable ``<name>.md`` files and a compact ``_index.json``
metadata file. Mutations are serialized with :func:`exclusive_file_lock`,
written atomically via ``os.replace``, redacted before persistence, and guarded
against symlinks, Windows reparse points, hard links, and quota overflow.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sonder_runtime.adapters.filesystem.durable_locks import exclusive_file_lock
from sonder_runtime.domain.memory.scratchpad import (
    VALID_WRITE_MODES,
    ScratchpadPolicy,
    normalize_owner,
    normalize_tags,
    preview_line,
    validate_note_name,
    validate_positive_int,
)
from sonder_runtime.domain.security import redaction
from sonder_runtime.platform.logging import Redactor
from sonder_runtime.platform.private_files import (
    ensure_private_dir,
    prepare_private_file,
)


class ScratchpadError(ValueError):
    """Caller-facing scratchpad validation or storage safety error."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _check_path(path: Path, stop_at: Path) -> None:
    """Refuse symlinks, Windows reparse points, and hard-linked files inside root."""
    candidates: list[Path] = []
    cur = path
    while True:
        candidates.append(cur)
        if cur == stop_at or cur.parent == cur:
            break
        cur = cur.parent
    for candidate in candidates:
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or (getattr(info, "st_file_attributes", 0) & 0x400):
            raise ScratchpadError("scratchpad paths must not contain links or reparse points")
        if candidate == path and stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
            raise ScratchpadError("scratchpad files must not be hard linked")


def _write_atomic(path: Path, data: bytes, *, root: Path, limit: int) -> None:
    if len(data) > limit:
        raise ScratchpadError(f"scratchpad payload exceeds {limit}-byte limit")
    _check_path(path, root)
    ensure_private_dir(path.parent)
    fd, tmp_name = tempfile.mkstemp(prefix=".scratch-", suffix=".tmp", dir=path.parent)
    try:
        prepare_private_file(tmp_name, create=False)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        _check_path(path, root)
        os.replace(tmp_name, path)
        prepare_private_file(path, create=False)
    finally:
        if os.path.exists(tmp_name):
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


def _read_bounded_text(path: Path, *, root: Path, limit: int) -> str:
    _check_path(path, root)
    with path.open("rb") as handle:
        raw = handle.read(limit + 1)
    if len(raw) > limit:
        raise ScratchpadError(f"scratchpad note exceeds {limit}-byte limit")
    return raw.decode("utf-8", errors="replace")


class ScratchpadStore:
    """Owner-scoped persistent Markdown scratchpad stored under ``<home>/scratchpad``."""

    def __init__(
        self,
        home: str | Path,
        policy: ScratchpadPolicy | None = None,
        *,
        redactor: Redactor | None = None,
    ) -> None:
        self.home = Path(home)
        self.root = self.home / "scratchpad"
        self.policy = policy or ScratchpadPolicy()
        self._redactor = redactor

    def _redact(self, text: str) -> str:
        cleaned = redaction.redact_text(text)
        if self._redactor is not None:
            cleaned = self._redactor.redact(cleaned)
        return cleaned

    def owner_dir(self, owner: str | None) -> Path:
        return self.root / normalize_owner(owner)

    def _note_path(self, odir: Path, name: str) -> Path:
        validated = validate_note_name(name, self.policy)
        candidate = odir / f"{validated}.md"
        if candidate.parent != odir:
            raise ScratchpadError("invalid scratchpad note path")
        return candidate

    @contextmanager
    def _locked(self, odir: Path) -> Iterator[None]:
        _check_path(self.root, self.home)
        ensure_private_dir(self.root)
        _check_path(odir, self.root)
        ensure_private_dir(odir)
        lock_path = odir / ".scratchpad.lock"
        _check_path(lock_path, self.root)
        with exclusive_file_lock(lock_path, timeout=10.0):
            yield

    def _load_index(self, odir: Path) -> dict[str, dict[str, Any]]:
        idx_path = odir / "_index.json"
        if not idx_path.exists():
            return {}
        try:
            raw = _read_bounded_text(idx_path, root=self.root, limit=512 * 1024)
            parsed = json.loads(raw)
        except (OSError, ValueError, ScratchpadError):
            return {}
        notes = parsed.get("notes") if isinstance(parsed, dict) else None
        if not isinstance(notes, dict):
            return {}
        return {str(k): dict(v) for k, v in notes.items() if isinstance(v, dict)}

    def _save_index(self, odir: Path, notes: dict[str, dict[str, Any]]) -> None:
        payload = json.dumps({"version": 1, "notes": notes}, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
        _write_atomic(odir / "_index.json", payload, root=self.root, limit=512 * 1024)

    def _reconcile_entries(self, odir: Path) -> dict[str, dict[str, Any]]:
        """Reconcile ``_index.json`` against on-disk ``*.md`` files (picks up human edits too)."""
        indexed = self._load_index(odir)
        result: dict[str, dict[str, Any]] = {}
        if not odir.is_dir():
            return result
        for entry in sorted(odir.iterdir(), key=lambda p: p.name):
            if not entry.name.endswith(".md") or entry.name.startswith(".") or entry.name.startswith("_"):
                continue
            stem = entry.name[:-3]
            try:
                name = validate_note_name(stem, self.policy)
                _check_path(entry, self.root)
                st = entry.lstat()
            except (ValueError, OSError, ScratchpadError):
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            meta = dict(indexed.get(name, {}))
            size_bytes = int(st.st_size)
            mtime_iso = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            if meta.get("size_bytes") != size_bytes or not meta.get("preview"):
                try:
                    text = _read_bounded_text(entry, root=self.root, limit=self.policy.max_note_bytes)
                    meta["preview"] = preview_line(text, self.policy.preview_chars)
                    meta["sha256"] = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
                    meta["lines"] = len(text.splitlines())
                except (OSError, ScratchpadError):
                    meta["preview"] = ""
                    meta["lines"] = 0
            meta["name"] = name
            meta["size_bytes"] = size_bytes
            meta["tags"] = list(meta.get("tags") or [])
            meta["created_at"] = str(meta.get("created_at") or mtime_iso)
            meta["updated_at"] = str(meta.get("updated_at") or mtime_iso)
            result[name] = meta
        return result

    def write(
        self,
        owner: str | None,
        name: str,
        content: str,
        *,
        mode: str = "replace",
        tags: list[str] | tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        """Create, replace, or append to a named persistent scratchpad note."""
        if not isinstance(content, str):
            raise ScratchpadError("content must be a string")
        clean_mode = str(mode or "replace").strip().lower()
        if clean_mode not in VALID_WRITE_MODES:
            raise ScratchpadError("mode must be 'replace' or 'append'")
        validated_name = validate_note_name(name, self.policy)
        clean_tags = normalize_tags(tags, self.policy) if tags is not None else None
        redacted_chunk = self._redact(content)

        odir = self.owner_dir(owner)
        with self._locked(odir):
            entries = self._reconcile_entries(odir)
            note_path = self._note_path(odir, validated_name)
            existed = validated_name in entries and note_path.is_file()

            if not existed and len(entries) >= self.policy.max_notes_per_owner:
                raise ScratchpadError(
                    f"scratchpad note limit ({self.policy.max_notes_per_owner}) reached; delete or reuse an existing note"
                )

            if clean_mode == "append" and existed:
                existing_text = _read_bounded_text(note_path, root=self.root, limit=self.policy.max_note_bytes)
                sep = "" if (not existing_text or existing_text.endswith("\n") or not redacted_chunk) else "\n"
                final_text = existing_text + sep + redacted_chunk
            else:
                final_text = redacted_chunk

            final_bytes = final_text.encode("utf-8")
            if len(final_bytes) > self.policy.max_note_bytes:
                raise ScratchpadError(
                    f"scratchpad note {validated_name!r} would be {len(final_bytes)} bytes "
                    f"(limit {self.policy.max_note_bytes})"
                )

            other_total = sum(
                int(m.get("size_bytes", 0)) for k, m in entries.items() if k != validated_name
            )
            if other_total + len(final_bytes) > self.policy.max_total_bytes_per_owner:
                raise ScratchpadError(
                    f"owner scratchpad quota ({self.policy.max_total_bytes_per_owner} bytes) exceeded"
                )

            _write_atomic(note_path, final_bytes, root=self.root, limit=self.policy.max_note_bytes)
            now = _utc_now()
            prev = entries.get(validated_name, {})
            merged_tags = list(clean_tags) if clean_tags is not None else list(prev.get("tags") or [])
            meta = {
                "name": validated_name,
                "size_bytes": len(final_bytes),
                "lines": len(final_text.splitlines()),
                "tags": merged_tags,
                "created_at": str(prev.get("created_at") or now),
                "updated_at": now,
                "sha256": hashlib.sha256(final_bytes).hexdigest()[:16],
                "preview": preview_line(final_text, self.policy.preview_chars),
            }
            entries[validated_name] = meta
            self._save_index(odir, entries)
            return {
                "ok": True,
                "action": "write",
                "mode": clean_mode,
                "created": not existed,
                "note": meta,
                "path": str(note_path),
            }

    def read(
        self,
        owner: str | None,
        name: str,
        *,
        start_line: int = 1,
        max_lines: int = 200,
    ) -> dict[str, Any]:
        """Read one scratchpad note with bounded line slicing."""
        validated_name = validate_note_name(name, self.policy)
        start = validate_positive_int("start_line", start_line, minimum=1, maximum=100_000)
        limit = validate_positive_int("max_lines", max_lines, minimum=1, maximum=self.policy.max_read_lines)
        odir = self.owner_dir(owner)
        if not odir.is_dir():
            return {"ok": False, "action": "read", "name": validated_name, "error": f"scratchpad note {validated_name!r} not found"}
        _check_path(odir, self.root)
        note_path = self._note_path(odir, validated_name)
        if not note_path.is_file():
            return {"ok": False, "action": "read", "name": validated_name, "error": f"scratchpad note {validated_name!r} not found"}
        text = _read_bounded_text(note_path, root=self.root, limit=self.policy.max_note_bytes)
        all_lines = text.splitlines()
        total_lines = len(all_lines)
        slice_lines = all_lines[start - 1 : start - 1 + limit]
        end_line = start + len(slice_lines) - 1 if slice_lines else start - 1
        truncated = (start - 1 + limit) < total_lines
        entries = self._reconcile_entries(odir)
        meta = entries.get(validated_name, {"name": validated_name, "size_bytes": len(text.encode("utf-8")), "tags": []})
        return {
            "ok": True,
            "action": "read",
            "note": meta,
            "start_line": start,
            "end_line": end_line,
            "total_lines": total_lines,
            "truncated": truncated,
            "content": "\n".join(slice_lines),
            "framing": "scratchpad_working_notes_reference_only",
        }

    def list_notes(self, owner: str | None, *, tag: str = "") -> dict[str, Any]:
        """List all scratchpad notes belonging to the owner, newest first."""
        filter_tag = str(tag or "").strip().lower()
        odir = self.owner_dir(owner)
        if not odir.is_dir():
            return {"ok": True, "action": "list", "notes": [], "count": 0, "total_bytes": 0}
        _check_path(odir, self.root)
        entries = self._reconcile_entries(odir)
        items = list(entries.values())
        if filter_tag:
            items = [m for m in items if filter_tag in (m.get("tags") or [])]
        items.sort(key=lambda m: (str(m.get("updated_at") or ""), str(m.get("name") or "")), reverse=True)
        total_bytes = sum(int(m.get("size_bytes", 0)) for m in entries.values())
        return {
            "ok": True,
            "action": "list",
            "notes": items,
            "count": len(items),
            "total_bytes": total_bytes,
            "quota_bytes": self.policy.max_total_bytes_per_owner,
        }

    def search(self, owner: str | None, query: str, *, limit: int = 10) -> dict[str, Any]:
        """Case-insensitive substring search across the owner's scratchpad notes."""
        needle = str(query or "").strip()
        if not needle:
            raise ScratchpadError("search query must be non-empty")
        max_hits = validate_positive_int("limit", limit, minimum=1, maximum=self.policy.max_search_results)
        odir = self.owner_dir(owner)
        if not odir.is_dir():
            return {"ok": True, "action": "search", "query": needle, "matches": [], "count": 0}
        _check_path(odir, self.root)
        entries = self._reconcile_entries(odir)
        needle_lower = needle.lower()
        matches: list[dict[str, Any]] = []
        for name, meta in sorted(
            entries.items(),
            key=lambda kv: (str(kv[1].get("updated_at") or ""), kv[0]),
            reverse=True,
        ):
            note_path = self._note_path(odir, name)
            try:
                text = _read_bounded_text(note_path, root=self.root, limit=self.policy.max_note_bytes)
            except (OSError, ScratchpadError):
                continue
            for idx, line in enumerate(text.splitlines(), start=1):
                if needle_lower in line.lower():
                    matches.append({
                        "name": name,
                        "line": idx,
                        "snippet": line.strip()[: self.policy.preview_chars],
                        "tags": list(meta.get("tags") or []),
                    })
                    if len(matches) >= max_hits:
                        return {
                            "ok": True,
                            "action": "search",
                            "query": needle,
                            "matches": matches,
                            "count": len(matches),
                            "truncated": True,
                        }
        return {
            "ok": True,
            "action": "search",
            "query": needle,
            "matches": matches,
            "count": len(matches),
            "truncated": False,
        }

    def delete(self, owner: str | None, name: str) -> dict[str, Any]:
        """Delete one named note from the owner's scratchpad."""
        validated_name = validate_note_name(name, self.policy)
        odir = self.owner_dir(owner)
        if not odir.is_dir():
            return {"ok": True, "action": "delete", "name": validated_name, "deleted": False}
        with self._locked(odir):
            entries = self._reconcile_entries(odir)
            note_path = self._note_path(odir, validated_name)
            deleted = False
            if note_path.exists():
                _check_path(note_path, self.root)
                note_path.unlink()
                deleted = True
            if validated_name in entries:
                entries.pop(validated_name, None)
                deleted = True
            self._save_index(odir, entries)
            return {"ok": True, "action": "delete", "name": validated_name, "deleted": deleted}


__all__ = ["ScratchpadError", "ScratchpadStore"]
