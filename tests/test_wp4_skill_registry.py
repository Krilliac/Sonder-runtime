"""WP4 SKILL-001/002 progressive discovery contract tests."""

from pathlib import Path

from sonder_runtime.application.skill_discovery import ProgressiveSkillRegistry, SkillSource


def write_skill(root: Path, name: str, description: str, content: str) -> None:
    directory = root / name
    directory.mkdir()
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n{content}",
        encoding="utf-8",
    )


def test_discovery_is_validated_sorted_and_uses_precedence(tmp_path):
    bundled = tmp_path / "bundled"
    project = tmp_path / "project"
    bundled.mkdir()
    project.mkdir()
    write_skill(bundled, "zeta", "Bundled skill", "bundled body")
    write_skill(bundled, "shared", "Old description", "old body")
    write_skill(project, "alpha", "Project skill", "project body")
    write_skill(project, "shared", "Project description", "new body")
    write_skill(project, "Invalid", "Rejected", "bad body")

    registry = ProgressiveSkillRegistry((SkillSource("bundled", bundled), SkillSource("project", project)))

    assert [item.name for item in registry.discover()] == ["alpha", "shared", "zeta"]
    assert registry.discover()[1].description == "Project description"


def test_discover_does_not_expose_or_read_full_content(tmp_path):
    root = tmp_path / "skills"
    root.mkdir()
    write_skill(root, "review", "Review code", "PRIVATE INSTRUCTIONS")
    registry = ProgressiveSkillRegistry((SkillSource("configured", root),))

    summary = registry.discover()[0]
    assert summary.name == "review"
    assert "PRIVATE" not in summary.description
    assert registry.skill("review").endswith("PRIVATE INSTRUCTIONS")


def test_missing_and_malformed_sources_are_ignored(tmp_path):
    root = tmp_path / "skills"
    root.mkdir()
    (root / "malformed").mkdir()
    (root / "malformed" / "SKILL.md").write_text("not a manifest", encoding="utf-8")

    registry = ProgressiveSkillRegistry((SkillSource("global", root / "missing"), SkillSource("project", root)))

    assert registry.discover() == ()
