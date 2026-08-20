"""Validate master-spec requirement IDs and the append-only evidence ledger."""
from __future__ import annotations

import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "docs" / "architecture" / "SONDER-MASTER-IMPLEMENTATION-SPEC.md"
LEDGER = ROOT / "docs" / "architecture" / "evidence" / "requirements.jsonl"
ID_PATTERN = re.compile(r"\b([A-Z][A-Z0-9]+-[0-9]{3})\b")
CHECKBOX_PATTERN = re.compile(
    r"^\s*- \[(?P<checked>[ xX])\].*?\b(?P<id>[A-Z][A-Z0-9]+-[0-9]{3})\b"
)
STATUSES = {
    "planned", "in_progress", "blocked", "implemented_unverified",
    "verified", "regressed", "superseded", "rejected",
}
REQUIRED = {"schema", "requirement_id", "revision", "status", "claim"}
ALLOWED = REQUIRED | {
    "baseline_sha", "verified_sha", "pr", "evidence", "platforms",
    "limitations", "verified_at",
}


def validate() -> list[str]:
    problems: list[str] = []
    spec_rows: dict[str, bool] = {}
    all_ids: list[str] = []
    for number, line in enumerate(SPEC.read_text(encoding="utf-8").splitlines(), 1):
        all_ids.extend(ID_PATTERN.findall(line))
        match = CHECKBOX_PATTERN.match(line)
        if match:
            requirement_id = match.group("id")
            if requirement_id in spec_rows:
                problems.append(f"spec:{number}: duplicate requirement {requirement_id}")
            spec_rows[requirement_id] = match.group("checked").lower() == "x"

    for requirement_id, count in Counter(all_ids).items():
        if count > 1 and requirement_id not in spec_rows:
            problems.append(f"spec: repeated unindexed ID {requirement_id}")
    if set(spec_rows) != set(all_ids):
        missing = sorted(set(all_ids) - set(spec_rows))
        problems.append("spec: IDs outside requirement checkboxes: " + ", ".join(missing))

    records: dict[str, list[dict[str, object]]] = defaultdict(list)
    for number, raw in enumerate(LEDGER.read_text(encoding="utf-8").splitlines(), 1):
        if not raw or len(raw) > 16_384:
            problems.append(f"ledger:{number}: empty or oversized line")
            continue
        try:
            record = json.loads(raw)
        except json.JSONDecodeError as exc:
            problems.append(f"ledger:{number}: invalid JSON: {exc.msg}")
            continue
        if not isinstance(record, dict):
            problems.append(f"ledger:{number}: record must be an object")
            continue
        keys = set(record)
        if missing := REQUIRED - keys:
            problems.append(f"ledger:{number}: missing keys {sorted(missing)}")
        if extra := keys - ALLOWED:
            problems.append(f"ledger:{number}: unknown keys {sorted(extra)}")
        requirement_id = record.get("requirement_id")
        if requirement_id not in spec_rows:
            problems.append(f"ledger:{number}: unknown requirement {requirement_id!r}")
            continue
        if record.get("schema") != "sonder-requirement-evidence-v1":
            problems.append(f"ledger:{number}: unsupported schema")
        if record.get("status") not in STATUSES:
            problems.append(f"ledger:{number}: invalid status")
        revision = record.get("revision")
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
            problems.append(f"ledger:{number}: revision must be a positive integer")
        records[requirement_id].append(record)

    for requirement_id in sorted(spec_rows):
        rows = records.get(requirement_id, [])
        if not rows:
            problems.append(f"ledger: missing requirement {requirement_id}")
            continue
        revisions = [row.get("revision") for row in rows]
        if revisions != sorted(revisions) or len(revisions) != len(set(revisions)):
            problems.append(f"ledger: revisions not strictly increasing for {requirement_id}")
        latest = rows[-1]
        if spec_rows[requirement_id] and latest.get("status") != "verified":
            problems.append(f"spec: checked requirement {requirement_id} is not verified")
        if latest.get("status") == "verified":
            for key in ("baseline_sha", "verified_sha", "evidence"):
                if not latest.get(key):
                    problems.append(f"ledger: verified {requirement_id} lacks {key}")

    return problems


def main() -> int:
    problems = validate()
    for problem in problems:
        print(problem)
    if problems:
        print(f"\n{len(problems)} evidence violation(s)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
