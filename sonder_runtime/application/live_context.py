"""Authoritative, bounded producers for live agent prefix context."""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from .instruction_discovery.registry import (
    InstructionDiscoveryError,
    InstructionRegistry,
    InstructionSource,
)
from .skill_discovery.registry import ProgressiveSkillRegistry, SkillSource
from .context_manifests import ContextRecord


MAX_SKILLS = 128
MAX_CATALOG_BYTES = 128 * 1024


@dataclass(frozen=True)
class LiveContextResult:
    """One complete or explicitly incomplete producer snapshot."""

    workspace_root: Path
    records: tuple[ContextRecord, ...]
    complete: bool
    reason: str = "complete"
    digest: str = ""


class LiveAgentContextProducer:
    """Discover trusted-in-scope rules and skill metadata for one lane.

    The caller supplies only explicitly configured roots.  The lane workspace
    is appended at highest precedence for project-local overrides.  A failed
    refresh can reuse the last complete snapshot for the same workspace, but
    the result remains observable through ``reason``.
    """

    def __init__(
        self,
        *,
        instruction_roots: Mapping[str, Sequence[Path | str]] | None = None,
        skill_roots: Mapping[str, Sequence[Path | str]] | None = None,
        max_skills: int = MAX_SKILLS,
        max_catalog_bytes: int = MAX_CATALOG_BYTES,
    ) -> None:
        if not 1 <= max_skills <= MAX_SKILLS:
            raise ValueError("max_skills is out of bounds")
        if not 1 <= max_catalog_bytes <= MAX_CATALOG_BYTES:
            raise ValueError("max_catalog_bytes is out of bounds")
        self._instruction_roots = self._normalize(instruction_roots or {})
        self._skill_roots = self._normalize(skill_roots or {})
        self._max_skills = max_skills
        self._max_catalog_bytes = max_catalog_bytes
        self._last_good: dict[Path, LiveContextResult] = {}

    @staticmethod
    def _normalize(values: Mapping[str, Sequence[Path | str]]) -> dict[str, tuple[Path, ...]]:
        return {
            str(kind): tuple(Path(root).resolve() for root in roots)
            for kind, roots in values.items()
        }

    @staticmethod
    def _sources(values: Mapping[str, tuple[Path, ...]], project: Path):
        sources = [
            InstructionSource(kind, root)
            for kind, roots in values.items()
            for root in roots
        ]
        sources.append(InstructionSource("project", project))
        return sources

    @staticmethod
    def _skill_sources(values: Mapping[str, tuple[Path, ...]], project: Path):
        sources = [
            SkillSource(kind, root)
            for kind, roots in values.items()
            for root in roots
        ]
        sources.append(SkillSource("project", project))
        return sources

    @staticmethod
    def _inside(path: Path, root: Path) -> bool:
        return path == root or root in path.parents

    def refresh(self, workspace_root: Path | str) -> LiveContextResult:
        supplied_root = Path(workspace_root)
        # Resolve only after checking the caller's path.  Checking the
        # resolved path would hide a symlink or Windows junction at the
        # workspace boundary.
        redirected = supplied_root.is_symlink() or getattr(
            supplied_root, "is_junction", lambda: False
        )()
        root = supplied_root.resolve()
        previous = self._last_good.get(root)
        try:
            if redirected or not root.is_dir():
                raise ValueError("workspace scope is unavailable")
            instruction = InstructionRegistry(
                self._sources(self._instruction_roots, root)
            )
            skills = ProgressiveSkillRegistry(
                self._skill_sources(self._skill_roots, root)
            )
            summaries = skills.discover()
            if len(summaries) > self._max_skills:
                raise ValueError("skill catalog exceeds entry limit")
            if any(
                summary.path.is_symlink()
                or not self._inside(summary.path.resolve(), root)
                and summary.source == "project"
                for summary in summaries
            ):
                raise ValueError("skill catalog contains an untrusted path")
            rule_records = instruction.records()
            skill_lines = tuple(
                f"{item.name}: {item.description} [{item.source}]"
                for item in summaries
            )
            skill_text = "\n".join(skill_lines)
            if len(skill_text.encode("utf-8")) > self._max_catalog_bytes:
                raise ValueError("skill catalog exceeds byte limit")
            records = tuple(
                ContextRecord(
                    f"rule:{record.name}", "policy", record.content,
                    f"project-rule:{record.source}", ordinal=index, stable=True,
                )
                for index, record in enumerate(rule_records)
            )
            if skill_lines:
                records += (ContextRecord(
                    "skill-catalog", "skills", skill_text,
                    "scoped-skill-catalog", ordinal=len(records), stable=True,
                ),)
            if not rule_records or not skill_lines:
                raise ValueError("authoritative project rules or skill catalog is absent")
            digest = sha256(
                "\n".join(record.content for record in records).encode("utf-8")
            ).hexdigest()
            result = LiveContextResult(root, records, True, "complete", digest)
            self._last_good[root] = result
            return result
        except (InstructionDiscoveryError, OSError, UnicodeError, ValueError) as exc:
            if previous is not None:
                return LiveContextResult(
                    root, previous.records, True,
                    "last_good:" + type(exc).__name__, previous.digest,
                )
            return LiveContextResult(root, (), False, type(exc).__name__, "")


__all__ = ["LiveAgentContextProducer", "LiveContextResult"]
