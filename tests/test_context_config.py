from pathlib import Path

import pytest

from sonder_runtime.application.live_context import LiveAgentContextProducer
from sonder_runtime.platform.config import ConfigError, load_config


def _write_project(root: Path, name: str, rule: str) -> Path:
    project = root / name
    project.mkdir()
    (project / "AGENTS.md").write_text(rule, encoding="utf-8")
    skill = project / "skill"
    skill.mkdir()
    (skill / "SKILL.md").write_text(
        "---\nname: skill\ndescription: configured skill\n---\n", encoding="utf-8"
    )
    return project


def test_context_roots_parse_and_preserve_source_precedence(tmp_path):
    bundled = _write_project(tmp_path, "bundled", "BUNDLED")
    configured = _write_project(tmp_path, "configured", "CONFIGURED")
    project = _write_project(tmp_path, "project", "PROJECT")
    config_file = tmp_path / "sonder.toml"
    config_file.write_text(
        "[context]\n"
        f"instruction_bundled_roots = [{str(bundled)!r}]\n"
        f"instruction_configured_roots = [{str(configured)!r}]\n"
        f"skill_bundled_roots = [{str(bundled)!r}]\n"
        f"skill_configured_roots = [{str(configured)!r}]\n",
        encoding="utf-8",
    )
    config = load_config(config_file)
    result = LiveAgentContextProducer.from_config(config).refresh(project)
    rendered = "\n".join(item.content for item in result.records)
    assert result.complete
    assert "CONFIGURED" in rendered
    assert "PROJECT" not in rendered
    assert "BUNDLED" not in rendered


def test_context_roots_require_absolute_non_link_paths(tmp_path):
    config_file = tmp_path / "sonder.toml"
    config_file.write_text(
        '[context]\ninstruction_global_roots = ["relative/root"]\n',
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="absolute non-link path"):
        load_config(config_file)


def test_context_root_lists_are_bounded(tmp_path):
    config_file = tmp_path / "sonder.toml"
    roots = ",".join(repr(str(tmp_path / f"root-{index}")) for index in range(17))
    config_file.write_text(
        f"[context]\ninstruction_global_roots = [{roots}]\n", encoding="utf-8"
    )
    with pytest.raises(ConfigError, match="exceeds 16 roots"):
        load_config(config_file)
