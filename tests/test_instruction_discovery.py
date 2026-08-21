from __future__ import annotations

import pytest

from sonder_runtime.application.instruction_discovery import (
    InstructionDiscoveryError,
    InstructionRegistry,
)


def test_known_instruction_files_use_source_precedence(tmp_path):
    bundled = tmp_path / "bundled"
    project = tmp_path / "project"
    bundled.mkdir()
    project.mkdir()
    (bundled / "AGENTS.md").write_text("bundled", encoding="utf-8")
    (project / "AGENTS.md").write_text("project", encoding="utf-8")
    (project / ".zero").mkdir()
    (project / ".zero" / "AGENTS.md").write_text("zero", encoding="utf-8")

    registry = InstructionRegistry.from_roots({"bundled": bundled, "project": project})

    assert len(registry) == 2
    records = registry.records()
    assert [record.name for record in records] == [".zero/AGENTS.md", "AGENTS.md"]
    assert registry.content() == "zero\n\nproject"
    assert records[1].source == "project"


def test_instruction_files_are_bounded_and_symlinks_are_ignored(tmp_path):
    (tmp_path / "AGENTS.md").write_text("x" * 20, encoding="utf-8")
    registry = InstructionRegistry.from_roots({"project": tmp_path}, max_bytes=20)
    assert len(registry) == 1
    if hasattr((tmp_path / "link"), "symlink_to"):
        try:
            (tmp_path / "link").symlink_to(tmp_path / "AGENTS.md")
        except (OSError, NotImplementedError):
            pass
        else:
            assert len(registry.__class__.from_roots({"project": tmp_path})) == 1

    with pytest.raises(InstructionDiscoveryError, match="byte limit"):
        InstructionRegistry.from_roots({"project": tmp_path}, max_bytes=10)
