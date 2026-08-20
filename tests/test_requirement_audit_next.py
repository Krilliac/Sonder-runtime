"""Integrity checks for the conservative formal-requirement audit."""
from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "docs/architecture/SONDER-MASTER-IMPLEMENTATION-SPEC.md"
LEDGER = ROOT / "docs/architecture/evidence/requirements.jsonl"
AUDIT = ROOT / "docs/architecture/REQUIREMENT-AUDIT-NEXT.md"
CHECKBOX = re.compile(r"^\s*- \[(?P<state>[ xX])\].*?\b(?P<id>[A-Z][A-Z0-9]+-\d{3})\b")
AUDIT_ROW = re.compile(
    r"^\| (?P<id>(?:SESSION|LOOP|SEAM|CTX|REPO|SKILL|AGENT|JOB|TOOL|EXEC|MEM|EVAL|MODEL|API|DATA|OPS|SEC|TRAIN|UPDATE|DOC)-\d{3}) "
    r"\| (?P<finding>PROVEN-CONTRACT|PARTIAL|MISSING) \|"
)
FAMILIES = {
    "SESSION", "LOOP", "SEAM", "CTX", "REPO", "SKILL", "AGENT", "JOB",
    "TOOL", "EXEC", "MEM", "EVAL", "MODEL", "API", "DATA", "OPS",
    "SEC", "TRAIN", "UPDATE", "DOC",
}


def spec_requirements() -> dict[str, bool]:
    rows: dict[str, bool] = {}
    for line in SPEC.read_text(encoding="utf-8").splitlines():
        match = CHECKBOX.match(line)
        if match:
            rows[match.group("id")] = match.group("state").lower() == "x"
    return rows


def test_audit_lists_every_requested_requirement() -> None:
    requirements = spec_requirements()
    requested = {
        requirement_id
        for requirement_id in requirements
        if requirement_id.split("-", 1)[0] in FAMILIES
    }
    audit_rows = {
        match.group("id"): match.group("finding")
        for match in map(AUDIT_ROW.match, AUDIT.read_text(encoding="utf-8").splitlines())
        if match
    }
    assert len(requested) == 163
    assert set(audit_rows) == requested
    assert Counter(audit_rows.values()) == Counter({
        "PROVEN-CONTRACT": 163,
        "PARTIAL": 0,
        "MISSING": 0,
    })


def test_formal_checkboxes_and_ledger_remain_unpromoted() -> None:
    requirements = spec_requirements()
    assert len(requirements) == 204
    checkbox_lines = [
        line for line in SPEC.read_text(encoding="utf-8").splitlines()
        if re.match(r"^\s*- \[[ xX]\]", line)
    ]
    assert len(checkbox_lines) == 250
    assert not any(requirements.values())

    latest: dict[str, dict[str, object]] = {}
    for raw in LEDGER.read_text(encoding="utf-8").splitlines():
        record = json.loads(raw)
        requirement_id = str(record["requirement_id"])
        if (
            requirement_id not in latest
            or int(record["revision"]) > int(latest[requirement_id]["revision"])
        ):
            latest[requirement_id] = record
    assert len(latest) == 204
    assert {record["status"] for record in latest.values()} == {"planned"}


def test_audit_explicitly_has_no_safe_checkbox_candidates() -> None:
    text = AUDIT.read_text(encoding="utf-8")
    assert "## Safe checkbox candidates" in text
    section = text.split("## Safe checkbox candidates", 1)[1].split(
        "## Required follow-up evidence", 1
    )[0]
    assert "**None.**" in section
    assert "formal checkboxes were not edited" in text.lower()
