"""The master-spec evidence ledger remains complete and fail-closed."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess

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


def test_verified_requirement_requires_master_checkbox(tmp_path):
    module = _checker()
    module.SPEC = tmp_path / "spec.md"
    module.LEDGER = tmp_path / "requirements.jsonl"
    module.SPEC.write_text(
        "- [ ] **TEST-001 — Demonstration.** An unchecked claim.\n",
        encoding="utf-8",
    )
    module.LEDGER.write_text(
        '{"schema":"sonder-requirement-evidence-v1",'
        '"requirement_id":"TEST-001","revision":1,'
        '"status":"verified","claim":"Demonstration.",'
        '"baseline_sha":"abc","verified_sha":"def",'
        '"evidence":[{"path":"tests/production/test_requirement_evidence.py"}]}\n',
        encoding="utf-8",
    )
    assert module.validate() == [
        "spec: verified requirement TEST-001 is not checked"
    ]


def test_verified_requirement_rejects_missing_evidence_path(tmp_path):
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
        '"status":"verified","claim":"Demonstration.",'
        '"baseline_sha":"abc","verified_sha":"def",'
        '"evidence":[{"path":"does/not/exist.py"}]}\n',
        encoding="utf-8",
    )
    assert module.validate() == [
        "ledger: verified TEST-001 evidence path is missing: does/not/exist.py"
    ]


def test_verified_requirement_rejects_evidence_outside_repository(tmp_path):
    module = _checker()
    module.SPEC = tmp_path / "spec.md"
    module.LEDGER = tmp_path / "requirements.jsonl"
    module.SPEC.write_text(
        "- [x] **TEST-001 — Demonstration.** A checked claim.\n",
        encoding="utf-8",
    )
    outside = tmp_path / "external-proof.txt"
    outside.write_text("untrusted", encoding="utf-8")
    module.LEDGER.write_text(
        '{"schema":"sonder-requirement-evidence-v1",'
        '"requirement_id":"TEST-001","revision":1,'
        '"status":"verified","claim":"Demonstration.",'
        '"baseline_sha":"abc","verified_sha":"def",'
        '"evidence":[{"path":' + json.dumps(str(outside)) + '}]}\n',
        encoding="utf-8",
    )
    assert module.validate() == [
        "ledger: verified TEST-001 has invalid evidence path"
    ]


def test_generated_status_rejects_stale_projection(tmp_path):
    module = _checker()
    module.SPEC = tmp_path / "spec.md"
    module.LEDGER = tmp_path / "requirements.jsonl"
    module.STATUS_JSON = tmp_path / "status.json"
    module.STATUS_MD = tmp_path / "status.md"
    module.SPEC.write_text(
        "- [ ] **TEST-001 — Demonstration.** A planned claim.\n",
        encoding="utf-8",
    )
    module.LEDGER.write_text(
        '{"schema":"sonder-requirement-evidence-v1",'
        '"requirement_id":"TEST-001","revision":1,'
        '"status":"planned","claim":"Demonstration."}\n',
        encoding="utf-8",
    )
    module.STATUS_JSON.write_text("{}\n", encoding="utf-8")
    module.STATUS_MD.write_text("stale\n", encoding="utf-8")
    assert module.generated_problems() == [
        "generated: requirement-status.json is missing or stale",
        "generated: requirement-status.md is missing or stale",
    ]


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def _base_diff_fixture(tmp_path: Path, *, revise: bool) -> tuple[object, Path]:
    module = _checker()
    module.ROOT = tmp_path
    module.SPEC = tmp_path / "docs/architecture/spec.md"
    module.LEDGER = tmp_path / "docs/architecture/evidence/requirements.jsonl"
    module.SPEC.parent.mkdir(parents=True)
    module.LEDGER.parent.mkdir(parents=True)
    evidence = tmp_path / "proof.txt"
    evidence.write_text("proof\n", encoding="utf-8")
    module.SPEC.write_text("- [ ] **TEST-001 — Demonstration.** Claim.\n", encoding="utf-8")
    module.LEDGER.write_text(
        '{"schema":"sonder-requirement-evidence-v1","requirement_id":"TEST-001",'
        '"revision":1,"status":"implemented_unverified","claim":"Claim."}\n',
        encoding="utf-8",
    )
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.invalid")
    _git(tmp_path, "config", "user.name", "Evidence Test")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "base")
    module.SPEC.write_text("- [x] **TEST-001 — Demonstration.** Claim.\n", encoding="utf-8")
    if revise:
        module.LEDGER.write_text(
            '{"schema":"sonder-requirement-evidence-v1","requirement_id":"TEST-001",'
            '"revision":2,"status":"verified","claim":"Claim.",'
            '"baseline_sha":"abc","verified_sha":"def",'
            '"evidence":[{"path":"proof.txt"}]}\n',
            encoding="utf-8",
        )
    else:
        module.LEDGER.write_text(
            '{"schema":"sonder-requirement-evidence-v1","requirement_id":"TEST-001",'
            '"revision":1,"status":"verified","claim":"Claim.",'
            '"baseline_sha":"abc","verified_sha":"def",'
            '"evidence":[{"path":"proof.txt"}]}\n',
            encoding="utf-8",
        )
    return module, tmp_path


def test_base_diff_accepts_new_checkbox_with_new_verified_revision(tmp_path):
    module, _ = _base_diff_fixture(tmp_path, revise=True)
    assert module.validate("HEAD") == []


def test_base_diff_rejects_checkbox_without_new_verified_revision(tmp_path):
    module, _ = _base_diff_fixture(tmp_path, revise=False)
    assert module.validate("HEAD") == [
        "base-diff: newly checked requirement TEST-001 lacks a newly added "
        "verified ledger revision with evidence"
    ]


def test_base_diff_rejects_unresolvable_ref(tmp_path):
    module, _ = _base_diff_fixture(tmp_path, revise=True)
    assert module.validate("missing-base") == [
        "base-ref: cannot resolve 'missing-base'"
    ]


def test_base_diff_rejects_empty_ref(tmp_path):
    module, _ = _base_diff_fixture(tmp_path, revise=True)
    assert module.validate("") == ["base-ref: cannot resolve ''"]
