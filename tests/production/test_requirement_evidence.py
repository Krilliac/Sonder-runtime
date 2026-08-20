"""The master-spec evidence ledger remains complete and fail-closed."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


def _checker():
    path = ROOT / "scripts" / "check_requirement_evidence.py"
    spec = importlib.util.spec_from_file_location("requirement_evidence", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_requirement_evidence_is_complete_and_valid():
    assert _checker().validate() == []


def test_checked_requirement_requires_verified_evidence(tmp_path):
    module = _checker()
    module.SPEC = tmp_path / "spec.md"
    module.LEDGER = tmp_path / "requirements.jsonl"
    module.SPEC.write_text(
        "- [x] **TEST-001 — Demonstration.** A checked claim.\n",
        encoding="utf-8",
    )
    module.LEDGER.write_text(
        '{"schema":"sonder-requirement-evidence-v1",'
        '"requirement_id":"TEST-001","revision":1,'
        '"status":"planned","claim":"Demonstration."}\n',
        encoding="utf-8",
    )
    assert module.validate() == [
        "spec: checked requirement TEST-001 is not verified"
    ]
