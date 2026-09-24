"""Lazy, deterministic skill discovery for WP4 SKILL-001/002.

This is intentionally separate from ``application.skills`` (the WP3 seam).
Discovery reads only small manifests. Full SKILL.md content is read by
``skill`` after a caller has selected a validated name.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Iterable


_NAME = re.compile(r"^[a-z][a-z0-9._-]{0,127}$")
_DEFAULT_ORDER = ("bundled", "global", "project", "configured")
_MAX_MANIFEST_HEADER_BYTES = 16 * 1024


class SkillManifestIncomplete(ValueError):
    """A scoped skill candidate could not be included in a complete catalog."""


@dataclass(frozen=True, slots=True)
class SkillSource:
    """A discovery root; later entries take precedence over earlier ones."""

    kind: str
    root: Path


@dataclass(frozen=True, slots=True)
class SkillSummary:
    """The safe, concise representation exposed by ``discover``."""

    name: str
    description: str
    source: str
    path: Path


@dataclass(frozen=True, slots=True)
class _Manifest:
    summary: SkillSummary
    content_path: Path


class ProgressiveSkillRegistry:
    """Discover validated skill manifests and load content on demand.

    ``sources`` are ordered from lowest to highest precedence. Duplicate names
    resolve to the later source, and all output is sorted by name. Scanning is
    safe for missing roots and never reads full skill content.
    """

    def __init__(self, sources: Iterable[SkillSource] = (), *, max_entries: int = 128,
                 require_complete: bool = False) -> None:
        if type(max_entries) is not int or not 1 <= max_entries <= 128:
            raise ValueError("max_entries is out of bounds")
        if type(require_complete) is not bool:
            raise ValueError("require_complete must be a boolean")
        self._sources = tuple(sources)
        self._max_entries = max_entries
        self._require_complete = require_complete
        self._catalog: dict[str, _Manifest] = {}
        self.refresh()

    @classmethod
    def from_roots(cls, roots: dict[str, Path | str]) -> "ProgressiveSkillRegistry":
        """Build a registry in canonical bundled-to-configured precedence."""
        sources = [SkillSource(kind, Path(roots[kind])) for kind in _DEFAULT_ORDER if kind in roots]
        sources.extend(SkillSource(kind, Path(root)) for kind, root in roots.items() if kind not in _DEFAULT_ORDER)
        return cls(sources)

    def refresh(self) -> None:
        """Replace the catalog with a complete, deterministic manifest scan."""
        catalog: dict[str, _Manifest] = {}
        for source in self._sources:
            root = source.root
            if not root.is_dir():
                continue
            for content_path in sorted(root.glob("*/SKILL.md"), key=lambda path: path.as_posix()):
                manifest = _read_manifest(content_path, source.kind)
                if manifest is not None:
                    catalog[manifest.summary.name] = manifest
                    if len(catalog) > self._max_entries:
                        raise ValueError("skill catalog exceeds entry limit")
                elif self._require_complete:
                    # Generic discovery deliberately ignores invalid entries.
                    # A scoped model prefix instead needs a complete view of
                    # every SKILL.md candidate under its selected roots. A
                    # valid manifest can name a skill independently of its
                    # parent directory, so directory spelling cannot decide
                    # whether a previously visible skill has disappeared.
                    # Validate all candidates before claiming a
                    # stable catalog. Do not expose path or manifest content
                    # in the diagnostic that reaches the model request.
                    raise SkillManifestIncomplete("scoped skill manifest is incomplete")
        self._catalog = catalog

    def discover(self, query: str = "") -> tuple[SkillSummary, ...]:
        """Return only validated names and concise descriptions."""
        needle = query.strip().casefold()
        summaries = (manifest.summary for manifest in self._catalog.values())
        if needle:
            summaries = (item for item in summaries if needle in (item.name + " " + item.description).casefold())
        return tuple(sorted(summaries, key=lambda item: item.name))

    def skill(self, name: str) -> str:
        """Load full content for one discovered skill, or raise ``KeyError``."""
        try:
            manifest = self._catalog[name]
        except KeyError as exc:
            raise KeyError(f"unknown skill: {name}") from exc
        return manifest.content_path.read_text(encoding="utf-8")

    def __len__(self) -> int:
        return len(self._catalog)


def _read_manifest(path: Path, source: str) -> _Manifest | None:
    lines: list[str] = []
    with path.open("r", encoding="utf-8") as stream:
        if stream.readline(_MAX_MANIFEST_HEADER_BYTES + 1) != "---\n":
            return None
        remaining = _MAX_MANIFEST_HEADER_BYTES - 4
        while remaining > 0:
            line = stream.readline(remaining + 1)
            if not line:
                return None
            remaining -= len(line.encode("utf-8"))
            if remaining < 0:
                return None
            if line.rstrip("\r\n") == "---":
                break
            lines.append(line.rstrip("\r\n"))
        else:
            return None
    fields: dict[str, str] = {}
    for line in lines:
        key, separator, value = line.partition(":")
        if separator and key.strip() in {"name", "description"}:
            fields[key.strip()] = value.strip().strip("\"'")
    name = fields.get("name", "")
    description = fields.get("description", "")
    if not _NAME.fullmatch(name) or not description:
        return None
    concise = " ".join(description.split())[:240].rstrip()
    return _Manifest(SkillSummary(name, concise, source, path), path)
