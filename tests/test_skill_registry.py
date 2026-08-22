import pytest

from sonder_runtime.application.skills import SkillRegistry
from sonder_runtime.domain.skills import (
    DiscoveryLevel,
    SkillRecord,
    SkillSourceMetadata,
    SkillTrust,
    validate_skill,
)


def skill(**changes):
    values = {
        "skill_id": "repo-review",
        "name": "Repository review",
        "summary": "Review a repository",
        "source": SkillSourceMetadata(
            kind="local",
            locator="skills/repo-review/SKILL.md",
            revision="r1",
        ),
        "level": DiscoveryLevel.CONTENT,
        "content": "Review instructions",
        "validation": None,
        "trust": SkillTrust.LOCAL,
        "trusted": True,
        "policy_allowed": True,
    }
    values.update(changes)
    values.pop("validation", None)
    return SkillRecord(**values)


def test_progressive_discovery_hides_content_until_requested():
    registry = SkillRegistry([skill()])

    index = registry.discover(level=DiscoveryLevel.INDEX)
    metadata = registry.get("repo-review", DiscoveryLevel.METADATA)
    content = registry.get("repo-review", DiscoveryLevel.CONTENT)

    assert index[0].content is None
    assert index[0].source.locator.endswith("SKILL.md")
    assert metadata.content is None
    assert content.content == "Review instructions"


def test_validation_does_not_grant_trust_or_policy():
    checked = validate_skill(skill(trusted=False, policy_allowed=False, trust=SkillTrust.UNTRUSTED))

    assert checked.validation.valid
    assert not checked.is_usable
    assert checked.trust is SkillTrust.UNTRUSTED


def test_invalid_records_are_not_published():
    with pytest.raises(ValueError, match="skill_id"):
        SkillRegistry([skill(skill_id="Bad ID")])


def test_discovery_is_sorted_filtered_and_can_exclude_denied():
    denied = skill(skill_id="a-denied", policy_allowed=False)
    allowed = skill(skill_id="z-allowed", summary="Python review", policy_allowed=True)
    registry = SkillRegistry([allowed, denied])

    assert [item.skill_id for item in registry.discover(query="python")] == ["z-allowed"]
    assert [item.skill_id for item in registry.discover(include_denied=False)] == ["z-allowed"]
