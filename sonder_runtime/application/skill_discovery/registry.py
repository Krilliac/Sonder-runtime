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

    def __init__(self, sources: Iterable[SkillSource] = ()) -> None:
        self._sources = tuple(sources)
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
        if stream.readline() != "---\n":
            return None
        for line in stream:
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
