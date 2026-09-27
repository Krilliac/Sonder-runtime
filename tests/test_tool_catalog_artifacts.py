"""Freshness and gap tests for generated runtime catalog artifacts."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

from scripts import generate_documentation_catalogs as docs_catalogs
from sonder_runtime.application.ports.tool_registry import InMemoryToolRegistry, ToolDescriptor
from sonder_runtime.application.tools.catalog_artifacts import (
    check_catalog_artifacts,
    write_catalog_artifacts,
)
from sonder_runtime.application.tools.generated_catalogs import PERMISSIONS_NOTE, GeneratedCatalogs
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


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "generate_runtime_catalogs.py"


def _runtime_check(output):
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--runtime", "--output", str(output), "--check"],
        cwd=str(ROOT), capture_output=True, text=True, timeout=120,
    )


def test_committed_runtime_catalogs_are_fresh_against_the_live_registry():
    committed = docs_catalogs.RUNTIME_CATALOGS
    bundle = docs_catalogs.runtime_catalog_bundle()
    assert check_catalog_artifacts(committed, bundle) == ()
    # The documentation freshness gate CI runs covers every catalog file.
    expected = docs_catalogs.expected()
    for name in (*("mcp.json", "openai.json", "cli.json", "client.json",
                   "permissions.json", "conformance.json"), "manifest.json"):
        assert committed / name in expected, name
    # The permissions projection is built from descriptors that carry effects.
    assert '"write_files"' in (committed / "permissions.json").read_text(encoding="utf-8")
    # and says that an undeclared descriptor reads as pure with no effects.
    published = json.loads((committed / "permissions.json").read_text(encoding="utf-8"))
    assert published["note"] == PERMISSIONS_NOTE


def test_an_undeclared_descriptor_projects_as_pure_under_the_note():
    bundle = GeneratedCatalogs.generate(
        InMemoryToolRegistry((ToolDescriptor("run_anything", "Runs a program"),)),
        commands=("help",), event_kinds=(EventKind.TOOL_COMPLETED,),
    )
    (entry,) = bundle.permissions["tools"]
    assert entry == {"execution_class": "pure", "effects": [], "name": "run_anything"}
    note = bundle.permissions["note"]
    assert "'pure' with no effects" in note and "not evidence" in note
    assert "not the enforcement point" in note


def test_runtime_catalog_check_fails_when_a_descriptor_drifts(tmp_path):
    copy = tmp_path / "runtime-catalogs"
    shutil.copytree(docs_catalogs.RUNTIME_CATALOGS, copy)
    fresh = _runtime_check(copy)
    assert fresh.returncode == 0, fresh.stderr
    assert "runtime catalogs current" in fresh.stdout

    mcp = copy / "mcp.json"
    text = mcp.read_text(encoding="utf-8")
    first = text.index('"description": "') + len('"description": "')
    mcp.write_text(text[:first] + "drifted " + text[first:], encoding="utf-8")
    stale = _runtime_check(copy)
    assert stale.returncode == 1
    assert "mcp.json" in stale.stderr

    (copy / "manifest.json").unlink()
    missing = _runtime_check(copy)
    assert missing.returncode == 1 and "manifest.json" in missing.stderr
