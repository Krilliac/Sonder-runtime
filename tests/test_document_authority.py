from __future__ import annotations

import re
from pathlib import Path

from sonder_runtime.application.tools.generated_catalogs import GeneratedCatalogs
from sonder_runtime.application.ports.tool_registry import InMemoryToolRegistry


ROOT = Path(__file__).resolve().parents[1]
ARCH = ROOT / "docs" / "architecture"


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_authority_index_and_required_classifications_exist():
    required = (
        "docs/architecture/README.md",
        "docs/architecture/DOCUMENT-AUTHORITY-INDEX.md",
        "docs/architecture/adr/README.md",
        "docs/architecture/REMAINING-DOC-001-007.md",
    )
    for relative in required:
        assert (ROOT / relative).is_file(), relative

    index = _read("docs/architecture/DOCUMENT-AUTHORITY-INDEX.md")
    for phrase in (
        "historical/superseded",
        "historical/runbook",
        "Focused current-contract map",
        "GeneratedCatalogs",
        "stale-promise",
        "Formal requirement status",
    ):
        assert phrase in index


def test_historical_documents_are_explicitly_labeled_and_focused_paths_exist():
    index = _read("docs/architecture/DOCUMENT-AUTHORITY-INDEX.md")
    for relative in (
        "SPEC-5-End-State-Architecture.md",
        "SPEC-5-MIGRATION-RUNBOOK.md",
        "PROGRAM-STATUS.md",
    ):
        assert relative in index
        assert (ARCH / relative).is_file()

    for relative in (
        "ARCHITECTURE.md",
        "SECURITY.md",
        "SELFMOD.md",
        "TRAINING.md",
        "CLIENT.md",
        "MOBILE_HOST_CONTROL.md",
    ):
        assert (ROOT / relative).is_file(), relative


def test_new_adr_namespace_is_unique_and_historical_numbers_are_classified():
    policy = _read("docs/architecture/adr/README.md")
    assert "New ADRs belong under `docs/adr/`" in policy
    assert "ADR-YYYY-MM-DD-<slug>.md" in policy

    new_pattern = re.compile(r"^ADR-\d{4}-\d{2}-\d{2}-.+\.md$")
    new_names = [
        path.name
        for path in (ROOT / "docs" / "adr").glob("*.md")
        if new_pattern.fullmatch(path.name)
    ]
    assert len(new_names) == len(set(new_names))
    assert "existing `ADR-001` through `ADR-009` files here are retained" in policy


def test_generated_catalog_freshness_contract_is_discoverable_and_deterministic():
    index = _read("docs/architecture/DOCUMENT-AUTHORITY-INDEX.md")
    source = ROOT / "sonder_runtime" / "application" / "tools" / "generated_catalogs.py"
    assert source.is_file()
    assert "generated_catalogs.py" in index
    assert "SHA-256 freshness digest" in index
    assert "Configuration" in index

    first = GeneratedCatalogs.generate(InMemoryToolRegistry([]), event_kinds=[])
    second = GeneratedCatalogs.generate(InMemoryToolRegistry([]), event_kinds=[])
    assert first.digest == second.digest
    assert first.client["digest"] == first.digest


def test_stale_promise_inventory_is_explicit_and_formal_checkboxes_untouched():
    inventory = _read("docs/architecture/REMAINING-DOC-001-007.md")
    for category in ("Current", "Implemented foundation", "Planned/open", "Historical", "Limitation"):
        assert f"| {category} |" in inventory
    spec = _read("docs/architecture/SONDER-MASTER-IMPLEMENTATION-SPEC.md")
    for requirement in ("DOC-001", "DOC-002", "DOC-003", "DOC-004", "DOC-005", "DOC-006", "DOC-007"):
        assert re.search(rf"- \[ \] \*\*{requirement} —", spec)
