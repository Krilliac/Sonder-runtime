"""Pure SkillRegistry contract (WP3-SEAM-008).

This module describes skill data and validation only. It deliberately has no
filesystem, plugin, or legacy-loader dependency; adapters can map their own
discovery mechanisms into these types later.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
import re
from typing import Mapping


class DiscoveryLevel(str, Enum):
    """The amount of skill data a caller has requested."""

    INDEX = "index"
    METADATA = "metadata"
    CONTENT = "content"


class SkillTrust(str, Enum):
    """Policy-relevant trust states; validation never implies trust."""

    UNKNOWN = "unknown"
    UNTRUSTED = "untrusted"
    LOCAL = "local"
    VERIFIED = "verified"


@dataclass(frozen=True, slots=True)
class SkillSourceMetadata:
    """Provenance supplied by the discovery adapter."""

    kind: str
    locator: str
    provider: str = ""
    version: str = ""
    revision: str = ""
    digest: str = ""
    signature: str = ""


@dataclass(frozen=True, slots=True)
class ValidationResult:
    """Validation outcome kept separate from source trust."""

    valid: bool
    errors: tuple[str, ...] = ()
    checked_revision: str = ""


@dataclass(frozen=True, slots=True)
class SkillRecord:
    """A progressively materialized skill description."""

    skill_id: str
    name: str
    summary: str = ""
    source: SkillSourceMetadata = SkillSourceMetadata(kind="unknown", locator="")
    level: DiscoveryLevel = DiscoveryLevel.INDEX
    content: str | None = None
    version: str = ""
    compatibility: tuple[str, ...] = ()
    validation: ValidationResult = ValidationResult(valid=False)
    trust: SkillTrust = SkillTrust.UNKNOWN
    trusted: bool = False
    policy_allowed: bool = False

    @property
    def is_usable(self) -> bool:
        return self.validation.valid and self.trusted and self.policy_allowed

    def materialize(self, level: DiscoveryLevel, **changes) -> "SkillRecord":
        """Return a copy at a deeper discovery level, never mutating a record."""
        target = DiscoveryLevel(level)
        if target == DiscoveryLevel.CONTENT and changes.get("content") is None:
            raise ValueError("content is required at content discovery level")
        if target == DiscoveryLevel.INDEX:
            changes["content"] = None
        elif target == DiscoveryLevel.METADATA:
            changes["content"] = None
        return replace(self, level=target, **changes)


_SKILL_ID = re.compile(r"^[a-z][a-z0-9._-]{0,127}$")


def validate_skill(skill: SkillRecord, *, expected_revision: str | None = None) -> SkillRecord:
    """Validate structural fields and return a record with an explicit result.

    A valid record remains untrusted and policy-denied until an owning policy
    adapter explicitly sets those fields. This prevents discovery from becoming
    an execution or authorization path.
    """
    errors: list[str] = []
    if not isinstance(skill.skill_id, str) or not _SKILL_ID.fullmatch(skill.skill_id):
        errors.append("skill_id must be a lowercase identifier")
    if not isinstance(skill.name, str) or not skill.name.strip():
        errors.append("name must be non-empty")
    if not skill.source.kind.strip():
        errors.append("source.kind must be non-empty")
    if not skill.source.locator.strip():
        errors.append("source.locator must be non-empty")
    if skill.level == DiscoveryLevel.CONTENT and not isinstance(skill.content, str):
        errors.append("content is required at content discovery level")
    if expected_revision is not None and skill.source.revision != expected_revision:
        errors.append("source revision does not match expected revision")
    result = ValidationResult(
        valid=not errors,
        errors=tuple(errors),
        checked_revision=skill.source.revision,
    )
    return replace(skill, validation=result)
