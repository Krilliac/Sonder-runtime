"""Contract tests for documentation authority and generated references."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from scripts import check_documentation_authority as checker

ROOT = Path(__file__).resolve().parents[1]
GENERATED = ROOT / "docs" / "architecture" / "generated"


def test_authority_checker_passes_and_inventory_is_complete():
    assert checker.check() == []
    inventory = json.loads((GENERATED / "focused-contract-inventory.json").read_text(encoding="utf-8"))
    assert len(inventory["documents"]) == 6
    assert all(item["classification"] == "current" and item["exists"] for item in inventory["documents"])
    assert all(item["classification"] == "superseded" and item["exists"] for item in inventory["historical"])


def test_generated_runtime_reference_covers_available_metadata():
    reference = json.loads((GENERATED / "runtime-reference.json").read_text(encoding="utf-8"))
    assert reference["schema"] == "sonder-runtime-document-reference-v1"
    assert reference["counts"]["commands"] >= 300
    assert reference["counts"]["tools"] >= 204
    assert reference["counts"]["events"] >= 43
    assert reference["counts"]["configuration"] >= 65
    assert len({row["name"] for row in reference["tools"]}) == reference["counts"]["tools"]
    assert len({row["name"] for row in reference["commands"]}) == reference["counts"]["commands"]
    config_keys = {(row["section"], row["field"]) for row in reference["configuration"]}
    assert {("root", "schema_version"), ("secrets", "api_key"), ("server", "host")} <= config_keys
    assert len(reference["digest"]) == 64


def test_public_generator_freshness_check_passes():
    result = subprocess.run(
        [sys.executable, "scripts/generate_documentation_catalogs.py", "--check"],
        cwd=ROOT, text=True, capture_output=True, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_adr_namespace_accepts_historical_series_and_requires_date_prefix_for_new_adrs():
    policy = (ROOT / "docs" / "architecture" / "adr" / "README.md").read_text(encoding="utf-8")
    assert "New ADRs belong under `docs/adr/`" in policy
    assert "globally unique" in policy
    assert checker.DATE_ADR.fullmatch("ADR-2026-08-20-doc-authority.md")
    assert not checker.DATE_ADR.fullmatch("ADR-010-new-decision.md")
