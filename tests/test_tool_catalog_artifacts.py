"""Freshness and gap tests for generated runtime catalog artifacts."""
from __future__ import annotations

from sonder_runtime.application.ports.tool_registry import InMemoryToolRegistry, ToolDescriptor
from sonder_runtime.application.tools.catalog_artifacts import (
    check_catalog_artifacts,
    write_catalog_artifacts,
)
from sonder_runtime.application.tools.generated_catalogs import GeneratedCatalogs
from sonder_runtime.domain.common.events import EventKind
from sonder_runtime.domain.tools.descriptors import ExecutionClass, ToolEffect


def _bundle():
    return GeneratedCatalogs.generate(
        InMemoryToolRegistry((ToolDescriptor(
            "write_file", "Write a file", {"type": "object"},
            frozenset({ToolEffect.WRITE_FILES}), ExecutionClass.HOST,
        ),)),
        commands=("help",), event_kinds=(EventKind.TOOL_COMPLETED,),
    )


def test_artifact_set_contains_all_surfaces_permissions_and_conformance(tmp_path):
    bundle = _bundle()
    paths = write_catalog_artifacts(tmp_path, bundle)
    assert {path.name for path in paths} == {
        "mcp.json", "openai.json", "cli.json", "client.json",
        "permissions.json", "conformance.json", "manifest.json",
    }
    assert check_catalog_artifacts(tmp_path, bundle) == ()
    assert '"write_files"' in (tmp_path / "permissions.json").read_text()


def test_missing_or_changed_artifacts_are_a_freshness_failure(tmp_path):
    bundle = _bundle()
    write_catalog_artifacts(tmp_path, bundle)
    (tmp_path / "client.json").unlink()
    assert "client.json" in check_catalog_artifacts(tmp_path, bundle)
    write_catalog_artifacts(tmp_path, bundle)
    (tmp_path / "conformance.json").write_text("{}\n", encoding="utf-8")
    drift = check_catalog_artifacts(tmp_path, bundle)
    assert "conformance.json" in drift


def test_catalog_source_change_invalidates_artifacts(tmp_path):
    bundle = _bundle()
    write_catalog_artifacts(tmp_path, bundle)
    changed = GeneratedCatalogs.generate(
        InMemoryToolRegistry((ToolDescriptor("write_file", "changed"),)),
        commands=("help",), event_kinds=(EventKind.TOOL_COMPLETED,),
    )
    assert check_catalog_artifacts(tmp_path, changed)
