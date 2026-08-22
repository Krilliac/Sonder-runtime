"""In-memory SkillRegistry application service (WP3-SEAM-008)."""
from __future__ import annotations

from collections.abc import Iterable

from ...domain.skills.registry import DiscoveryLevel, SkillRecord, validate_skill


class SkillRegistry:
    """Own the discovered skill index without loading or executing skills.

    Registration is atomic: invalid records are rejected and never published.
    Discovery is deterministic and returns only the requested materialization
    level. Trust and policy are caller-supplied facts, not inferred here.
    """

    def __init__(self, skills: Iterable[SkillRecord] = ()) -> None:
        self._skills: dict[str, SkillRecord] = {}
        for skill in skills:
            self.register(skill)

    def register(self, skill: SkillRecord) -> SkillRecord:
        checked = validate_skill(skill)
        if not checked.validation.valid:
            raise ValueError("invalid skill: " + "; ".join(checked.validation.errors))
        if checked.skill_id in self._skills:
            raise ValueError(f"skill already registered: {checked.skill_id}")
        self._skills[checked.skill_id] = checked
        return checked

    def replace(self, skill: SkillRecord) -> SkillRecord:
        checked = validate_skill(skill)
        if not checked.validation.valid:
            raise ValueError("invalid skill: " + "; ".join(checked.validation.errors))
        if checked.skill_id not in self._skills:
            raise KeyError(checked.skill_id)
        self._skills[checked.skill_id] = checked
        return checked

    def get(self, skill_id: str, level: DiscoveryLevel = DiscoveryLevel.INDEX) -> SkillRecord | None:
        skill = self._skills.get(skill_id)
        if skill is None:
            return None
        requested = DiscoveryLevel(level)
        if requested == DiscoveryLevel.INDEX:
            return skill.materialize(requested, content=None)
        if requested == DiscoveryLevel.METADATA:
            return skill.materialize(requested, content=None)
        return skill if skill.level == DiscoveryLevel.CONTENT else None

    def discover(
        self,
        *,
        level: DiscoveryLevel = DiscoveryLevel.INDEX,
        query: str = "",
        include_denied: bool = True,
    ) -> tuple[SkillRecord, ...]:
        requested = DiscoveryLevel(level)
        needle = query.strip().casefold()
        found = []
        for skill in sorted(self._skills.values(), key=lambda item: item.skill_id):
            if not include_denied and not skill.policy_allowed:
                continue
            if needle and needle not in (skill.skill_id + " " + skill.name + " " + skill.summary).casefold():
                continue
            item = self.get(skill.skill_id, requested)
            if item is not None:
                found.append(item)
        return tuple(found)

    def __len__(self) -> int:
        return len(self._skills)
