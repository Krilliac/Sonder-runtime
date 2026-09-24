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


def test_stale_promise_inventory_tracks_verified_and_open_checkboxes():
    inventory = _read("docs/architecture/REMAINING-DOC-001-007.md")
    for category in ("Current", "Implemented foundation", "Planned/open", "Historical", "Limitation"):
        assert f"| {category} |" in inventory
    spec = _read("docs/architecture/SONDER-MASTER-IMPLEMENTATION-SPEC.md")
    assert re.search(r"- \[x\] \*\*DOC-006 —", spec, re.IGNORECASE)
    assert re.search(r"- \[x\] \*\*DOC-005 —", spec, re.IGNORECASE)
    for requirement in ("DOC-001", "DOC-002", "DOC-003", "DOC-004", "DOC-007"):
        assert re.search(rf"- \[x\] \*\*{requirement} —", spec)


def test_product_documents_satisfy_focus_and_status_vocabulary():
    assert checker._check_product_documents() == []
    for relative in checker.PRODUCT_DOCUMENTS:
        text = _read(relative)
        assert text.count(checker.STATUS_HEADING) == 1, relative
        assert "SONDER-MASTER-IMPLEMENTATION-SPEC.md" in text, relative
    readme = _read("README.md")
    assert "WP1 Forty-Fifth Slice" not in readme
    history = _read("docs/architecture/WP1-README-SLICE-LOG.md")
    assert "historical implementation history" in history
    assert "WP1 Forty-Fifth Slice" in history
    assert "WP1 Three-Hundred-Fifty-First Slice" in history


_SPEC_FIXTURE = """# Spec

- [ ] **ABC-001 — Open item.** Still open.
- [x] **ABC-002 — Done item.** Complete.
- [ ] **ABC-003 — Verified but unchecked.** Inconsistent ledger.
- [ ] **ABC-004 — Implemented foundation.** Unverified.
"""
_LEDGER_FIXTURE = "\n".join((
    '{"requirement_id":"ABC-001","revision":1,"status":"planned"}',
    '{"requirement_id":"ABC-002","revision":1,"status":"verified"}',
    '{"requirement_id":"ABC-003","revision":1,"status":"planned"}',
    '{"requirement_id":"ABC-003","revision":2,"status":"verified"}',
    '{"requirement_id":"ABC-004","revision":1,"status":"implemented_unverified"}',
)) + "\n"
_ROWS = """| Behavior | Status | Boundary |
|---|---|---|
| Shipped path | Implemented | Current (ABC-004). |
| Lab path | Experimental | Opt-in. |
| End state | Proposed | ABC-001 tracks it. |
| Fallback path | Degraded | Fails closed. |
| Absent path | Unsupported | Rejected. |
"""


def _product_fixture(tmp_path, monkeypatch, *, focused=None, readme=None):
    spec = tmp_path / "docs" / "architecture" / "SPEC.md"
    ledger = tmp_path / "docs" / "architecture" / "evidence" / "requirements.jsonl"
    ledger.parent.mkdir(parents=True)
    spec.write_text(_SPEC_FIXTURE, encoding="utf-8")
    ledger.write_text(_LEDGER_FIXTURE, encoding="utf-8")
    default_focused = (
        "# Focused\n\nScope: see [spec](docs/architecture/SPEC.md).\n\n"
        "Current prose.\n\n## Behavior status\n\n" + _ROWS
    )
    (tmp_path / "FOCUSED.md").write_text(focused or default_focused, encoding="utf-8")
    (tmp_path / "README.md").write_text(
        readme or "# Product\n\n## Behavior status\n\n" + _ROWS, encoding="utf-8"
    )
    monkeypatch.setattr(checker, "ROOT", tmp_path)
    monkeypatch.setattr(checker, "MASTER_SPEC", spec)
    monkeypatch.setattr(checker, "LEDGER", ledger)
    monkeypatch.setattr(checker, "FOCUSED_CONTRACTS", (("FOCUSED.md", "fixture"),))
    monkeypatch.setattr(checker, "PRODUCT_DOCUMENTS", ("README.md", "FOCUSED.md"))


def test_product_document_gate_accepts_complete_fixture(tmp_path, monkeypatch):
    _product_fixture(tmp_path, monkeypatch)
    assert checker._check_product_documents() == []


def test_focused_contract_must_link_master_spec_near_top(tmp_path, monkeypatch):
    focused = "# Focused\n\n" + "filler\n" * 12 + "[spec](docs/architecture/SPEC.md)\n\n## Behavior status\n\n" + _ROWS
    _product_fixture(tmp_path, monkeypatch, focused=focused)
    problems = checker._check_product_documents()
    assert any("FOCUSED.md: focused contract must link the master specification" in item for item in problems)


def test_status_rows_reject_unknown_labels_and_requirements(tmp_path, monkeypatch):
    readme = "# Product\n\n## Behavior status\n\n" + _ROWS + "| Soon path | Planned | ZZZ-999 later. |\n| Odd path | Implemented | Cites ZZZ-998. |\n"
    _product_fixture(tmp_path, monkeypatch, readme=readme)
    problems = checker._check_product_documents()
    assert "README.md: unknown status label 'Planned' for 'Soon path'" in problems
    assert "README.md: 'Odd path' cites unknown requirement ZZZ-998" in problems


def test_proposed_rows_become_stale_when_their_requirement_completes(tmp_path, monkeypatch):
    readme = "# Product\n\n## Behavior status\n\n" + _ROWS + (
        "| Checked promise | Proposed | ABC-002 tracks it. |\n"
        "| Verified promise | Proposed | ABC-003 tracks it. |\n"
        "| Untracked promise | Proposed | No requirement. |\n"
    )
    _product_fixture(tmp_path, monkeypatch, readme=readme)
    problems = checker._check_product_documents()
    for behavior in ("Checked promise", "Verified promise", "Untracked promise"):
        assert any(f"proposed behavior {behavior!r} must cite an open" in item for item in problems), behavior


def test_forward_looking_prose_and_slice_logs_are_rejected_outside_status(tmp_path, monkeypatch):
    readme = (
        "# Product\n\nStreaming resume is coming soon.\n"
        "- WP1 Forty-Fifth Slice: helper moved.\n"
        "# WP1 One-Hundred-Tenth Slice\n\n## Behavior status\n\n" + _ROWS
        + "| Offload | Unsupported | Not yet implemented; ABC-001. |\n"
    )
    _product_fixture(tmp_path, monkeypatch, readme=readme)
    problems = checker._check_product_documents()
    assert any("README.md:3: unlabeled forward-looking promise 'coming soon'" in item for item in problems)
    assert "README.md:4: implementation slice log belongs in historical records" in problems
    assert "README.md:5: implementation slice log belongs in historical records" in problems
    assert not any("Not yet implemented" in item for item in problems)


def test_status_vocabulary_must_be_exercised_and_sections_unique(tmp_path, monkeypatch):
    readme = "# Product\n\n## Behavior status\n\n| Behavior | Status | Boundary |\n|---|---|---|\n| Shipped | Implemented | Now. |\n\n## Behavior status\n"
    focused = "# Focused\n\n[spec](docs/architecture/SPEC.md)\n\n## Behavior status\n\n| Behavior | Status | Boundary |\n|---|---|---|\n| Shipped | Implemented | Now. |\n"
    _product_fixture(tmp_path, monkeypatch, readme=readme, focused=focused)
    problems = checker._check_product_documents()
    assert "README.md: needs exactly one '## Behavior status' section" in problems
    for label in ("Experimental", "Proposed", "Degraded", "Unsupported"):
        assert f"product documentation never uses the {label!r} status label" in problems


def test_check_reports_a_planted_bad_product_document(tmp_path, monkeypatch):
    """The product-document gate must be wired into ``check()`` itself."""
    readme = "# Product\n\nResumable streams are planned.\n\n## Behavior status\n\n" + _ROWS
    _product_fixture(tmp_path, monkeypatch, readme=readme)
    monkeypatch.setattr(checker, "HISTORICAL_DOCUMENTS", ())
    monkeypatch.setattr(checker, "_check_adr_namespace", lambda: [])
    monkeypatch.setattr(checker, "expected", lambda: {})
    problems = checker.check()
    assert any("README.md:3: unlabeled forward-looking promise 'planned'" in item for item in problems)


def test_status_rows_must_have_exactly_three_columns(tmp_path, monkeypatch):
    readme = "# Product\n\n## Behavior status\n\n" + _ROWS + "| Wide path | Implemented | Now. | Extra cell |\n"
    _product_fixture(tmp_path, monkeypatch, readme=readme)
    problems = checker._check_product_documents()
    assert any(
        item.startswith("README.md: status row needs Behavior | Status | Boundary")
        and "Extra cell" in item
        for item in problems
    )


def test_widened_promise_and_slice_patterns(tmp_path, monkeypatch):
    phrases = (
        "Sharding is upcoming.", "It will eventually ship.", "Resume is not yet wired.",
        "Streaming will land later.", "The API will be available soon.", "A planned mode.",
    )
    slices = ("WP1 Slice 12: moved a helper.", "- WP2 Three Hundred Slice: moved.", "## WP1 One-Hundred-Tenth Slice")
    readme = "# Product\n\n" + "\n".join(phrases + slices) + "\n\n## Behavior status\n\n" + _ROWS
    _product_fixture(tmp_path, monkeypatch, readme=readme)
    problems = checker._check_product_documents()
    for offset in range(len(phrases)):
        assert any(item.startswith(f"README.md:{3 + offset}: unlabeled forward-looking promise") for item in problems), phrases[offset]
    for offset in range(len(slices)):
        line = 3 + len(phrases) + offset
        assert f"README.md:{line}: implementation slice log belongs in historical records" in problems, slices[offset]
    assert not checker.STALE_PROMISE.search("Sonder is local by default.")
    assert not checker.SLICE_LOG_LINE.match("The WP1 program ended.")


def test_implemented_rows_must_cite_implemented_or_verified_requirements(tmp_path, monkeypatch):
    readme = "# Product\n\n## Behavior status\n\n" + _ROWS + "| Early claim | Implemented | Backed by ABC-001. |\n"
    _product_fixture(tmp_path, monkeypatch, readme=readme)
    problems = checker._check_product_documents()
    assert "README.md: implemented behavior 'Early claim' cites ABC-001 whose latest ledger status is 'planned'" in problems
    assert not any("'Shipped path'" in item for item in problems)


def test_thin_client_documentation_matches_its_checkout_dependency(tmp_path):
    """CLIENT.md must not promise a single-file client while it imports the package."""
    import os
    import shutil
    import subprocess
    import sys

    env = {
        key: value for key, value in os.environ.items()
        if key != "PYTHONPATH" and not key.startswith("SONDER_")
    }
    lone = tmp_path / "sonder_client.py"
    shutil.copy2(ROOT / "sonder_client.py", lone)
    alone = subprocess.run(
        [sys.executable, "-S", "-E", str(lone), "--help"],
        cwd=tmp_path, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60,
    )
    assert alone.returncode != 0
    assert "No module named 'sonder_runtime'" in alone.stderr
    in_checkout = subprocess.run(
        [sys.executable, "-S", "-E", str(ROOT / "sonder_client.py"), "--help"],
        cwd=ROOT, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60,
    )
    # With no SONDER_SERVER the client prints usage and exits 1: it imported fine.
    assert in_checkout.returncode == 1, in_checkout.stderr
    assert "ModuleNotFoundError" not in in_checkout.stderr
    assert "usage: sonder_client.py" in in_checkout.stdout

    client = _read("CLIENT.md")
    assert "stdlib-only" not in client
    assert "raw.githubusercontent.com" not in client
    assert "| Copying `sonder_client.py` alone to another machine | Unsupported |" in client
    assert "http://your-vps" not in _read("sonder_client.py")
