from __future__ import annotations

import re
from pathlib import Path

from scripts import check_documentation_authority as checker
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


def test_architecture_readme_is_a_direct_authority_map():
    readme = _read("docs/architecture/README.md")
    required_links = {
        "SONDER-MASTER-IMPLEMENTATION-SPEC.md": "Authoritative requirements",
        "../../ARCHITECTURE.md": "Authoritative current contract",
        "../../SECURITY.md": "Authoritative current contract",
        "../../SELFMOD.md": "Authoritative current contract",
        "../../TRAINING.md": "Authoritative current contract",
        "../../CLIENT.md": "Authoritative current contract",
        "../../MOBILE_HOST_CONTROL.md": "Authoritative current contract",
        "SPEC-5-End-State-Architecture.md": "Historical/superseded",
        "SPEC-5-MIGRATION-RUNBOOK.md": "Historical/runbook",
        "PROGRAM-STATUS.md": "Historical snapshot",
    }
    assert "## Direct authority map" in readme
    for link, classification in required_links.items():
        rows = [
            line for line in readme.splitlines()
            if line.startswith("|") and f"]({link})" in line
        ]
        assert len(rows) == 1, link
        cells = [cell.strip() for cell in rows[0].split("|")]
        assert cells[1] == classification, (link, rows[0])
        target = (ROOT / "docs" / "architecture" / link).resolve()
        assert target.is_file(), link


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
    canonical_policy = _read("docs/adr/README.md")
    assert "New ADRs belong under `docs/adr/`" in policy
    assert "ADR-YYYY-MM-DD-<slug>.md" in policy
    assert "New architecture decisions belong in this directory" in canonical_policy

    new_pattern = re.compile(r"^ADR-\d{4}-\d{2}-\d{2}-.+\.md$")
    new_names = [
        path.name
        for path in (ROOT / "docs" / "adr").glob("*.md")
        if new_pattern.fullmatch(path.name)
    ]
    assert len(new_names) == len(set(new_names))
    assert "existing `ADR-001` through `ADR-009` files here are retained" in policy


def test_adr_namespace_rejects_new_numeric_ids_and_historical_directory_writes(tmp_path, monkeypatch):
    canonical = tmp_path / "docs" / "adr"
    historical = tmp_path / "docs" / "architecture" / "adr"
    canonical.mkdir(parents=True)
    historical.mkdir(parents=True)
    for name in checker.LEGACY_CANONICAL_ADRS:
        (canonical / name).touch()
    for name in checker.LEGACY_ARCHITECTURE_ADRS:
        (historical / name).touch()
    monkeypatch.setattr(checker, "CANONICAL_ADR", canonical)
    monkeypatch.setattr(checker, "HISTORICAL_ADR", historical)

    (canonical / "ADR-2026-09-23-new-decision.md").touch()
    assert checker._check_adr_namespace() == []

    (canonical / "ADR-007-new-numeric-decision.md").touch()
    (historical / "ADR-010-new-historical-decision.md").touch()
    (canonical / "ADR-2026-99-99-invalid-date.md").touch()
    problems = checker._check_adr_namespace()
    assert any("ADR-007-new-numeric-decision.md: new ADR needs" in item for item in problems)
    assert any("ADR-010-new-historical-decision.md: historical ADR directory is frozen" in item for item in problems)
    assert any("ADR-2026-99-99-invalid-date.md: invalid ADR date" in item for item in problems)


def test_adr_namespace_rejects_missing_historical_record(tmp_path, monkeypatch):
    canonical = tmp_path / "docs" / "adr"
    historical = tmp_path / "docs" / "architecture" / "adr"
    canonical.mkdir(parents=True)
    historical.mkdir(parents=True)
    for name in checker.LEGACY_CANONICAL_ADRS:
        (canonical / name).touch()
    for name in checker.LEGACY_ARCHITECTURE_ADRS:
        (historical / name).touch()
    missing = sorted(checker.LEGACY_CANONICAL_ADRS)[0]
    (canonical / missing).unlink()
    monkeypatch.setattr(checker, "CANONICAL_ADR", canonical)
    monkeypatch.setattr(checker, "HISTORICAL_ADR", historical)

    assert f"docs/adr/{missing}: historical ADR is missing" in checker._check_adr_namespace()


def test_adr_namespace_rejects_nested_records_and_directories(tmp_path, monkeypatch):
    canonical = tmp_path / "docs" / "adr"
    historical = tmp_path / "docs" / "architecture" / "adr"
    canonical.mkdir(parents=True)
    historical.mkdir(parents=True)
    for name in checker.LEGACY_CANONICAL_ADRS:
        (canonical / name).touch()
    for name in checker.LEGACY_ARCHITECTURE_ADRS:
        (historical / name).touch()
    (canonical / "nested").mkdir()
    (canonical / "nested" / "ADR-2026-99-99-invalid-date.md").touch()
    (historical / "new-series").mkdir()
    (historical / "new-series" / "ADR-010-hidden.md").touch()
    (canonical / "figure.png").touch()
    monkeypatch.setattr(checker, "CANONICAL_ADR", canonical)
    monkeypatch.setattr(checker, "HISTORICAL_ADR", historical)

    problems = checker._check_adr_namespace()
    assert "docs/adr/nested: nested ADR directories are not permitted" in problems
    assert "docs/architecture/adr/new-series: nested ADR directories are not permitted" in problems
    assert not any("figure.png" in problem for problem in problems)


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


def test_stale_promise_inventory_is_explicit_and_unverified_checkboxes_remain_open():
    inventory = _read("docs/architecture/REMAINING-DOC-001-007.md")
    for category in ("Current", "Implemented foundation", "Planned/open", "Historical", "Limitation"):
        assert f"| {category} |" in inventory
    spec = _read("docs/architecture/SONDER-MASTER-IMPLEMENTATION-SPEC.md")
    assert re.search(r"- \[x\] \*\*DOC-006 —", spec, re.IGNORECASE)
    assert re.search(r"- \[x\] \*\*DOC-005 —", spec, re.IGNORECASE)
    for requirement in ("DOC-001", "DOC-002", "DOC-003", "DOC-004", "DOC-007"):
        assert re.search(rf"- \[ \] \*\*{requirement} —", spec)
