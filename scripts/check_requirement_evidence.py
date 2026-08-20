"""Validate master-spec requirement IDs and the append-only evidence ledger."""
from __future__ import annotations

import json
import hashlib
import re
import sys
from argparse import ArgumentParser
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "docs" / "architecture" / "SONDER-MASTER-IMPLEMENTATION-SPEC.md"
LEDGER = ROOT / "docs" / "architecture" / "evidence" / "requirements.jsonl"
GENERATED_DIR = ROOT / "docs" / "architecture" / "generated"
STATUS_JSON = GENERATED_DIR / "requirement-status.json"
STATUS_MD = GENERATED_DIR / "requirement-status.md"
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


def _latest_records() -> tuple[dict[str, bool], dict[str, dict[str, object]]]:
    """Return checkbox state and latest ledger record for each requirement."""
    checked: dict[str, bool] = {}
    for line in SPEC.read_text(encoding="utf-8").splitlines():
        match = CHECKBOX_PATTERN.match(line)
        if match:
            checked[match.group("id")] = match.group("checked").lower() == "x"
    latest: dict[str, dict[str, object]] = {}
    for raw in LEDGER.read_text(encoding="utf-8").splitlines():
        if not raw:
            continue
        record = json.loads(raw)
        requirement_id = record.get("requirement_id")
        if requirement_id not in checked:
            continue
        previous = latest.get(requirement_id)
        if previous is None or record.get("revision", 0) > previous.get("revision", 0):
            latest[requirement_id] = record
    return checked, latest


def generated_status() -> tuple[dict, str]:
    """Build deterministic JSON and Markdown status projections."""
    checked, latest = _latest_records()
    requirements = []
    families: dict[str, dict[str, int]] = {}
    for requirement_id in sorted(checked):
        record = latest[requirement_id]
        family = requirement_id.split("-", 1)[0]
        bucket = families.setdefault(
            family, {"total": 0, "checked": 0, "verified": 0}
        )
        bucket["total"] += 1
        bucket["checked"] += int(checked[requirement_id])
        bucket["verified"] += int(record.get("status") == "verified")
        requirements.append({
            "requirement_id": requirement_id,
            "claim": record.get("claim", ""),
            "status": record.get("status"),
            "checked": checked[requirement_id],
            "revision": record.get("revision"),
        })
    status_counts = Counter(item["status"] for item in requirements)
    source = {
        "spec_sha256": hashlib.sha256(SPEC.read_bytes()).hexdigest(),
        "ledger_sha256": hashlib.sha256(LEDGER.read_bytes()).hexdigest(),
    }
    payload = {
        "schema": "sonder-requirement-status-v1",
        "source": source,
        "overall": {
            "total": len(requirements),
            "checked": sum(item["checked"] for item in requirements),
            "verified": status_counts.get("verified", 0),
            "statuses": dict(sorted(status_counts.items())),
        },
        "families": dict(sorted(families.items())),
        "requirements": requirements,
    }
    encoded = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    markdown = [
        "# Master-spec requirement status",
        "",
        "Generated from the authoritative specification and evidence ledger.",
        "",
        "| Metric | Value |",
        "|---|---:|",
        f"| Requirements | {payload['overall']['total']} |",
        f"| Checked | {payload['overall']['checked']} |",
        f"| Verified evidence | {payload['overall']['verified']} |",
        "",
        "| Family | Total | Checked | Verified |",
        "|---|---:|---:|---:|",
    ]
    for family, counts in sorted(families.items()):
        markdown.append(
            f"| {family} | {counts['total']} | {counts['checked']} | {counts['verified']} |"
        )
    markdown.append("")
    return payload, "\n".join(markdown)


def generated_problems() -> list[str]:
    """Reject missing or stale generated projections without executing them."""
    payload, markdown = generated_status()
    problems: list[str] = []
    try:
        actual_json = STATUS_JSON.read_text(encoding="utf-8")
    except OSError:
        actual_json = None
    if actual_json != json.dumps(payload, indent=2, sort_keys=True) + "\n":
        problems.append("generated: requirement-status.json is missing or stale")
    try:
        actual_markdown = STATUS_MD.read_text(encoding="utf-8")
    except OSError:
        actual_markdown = None
    if actual_markdown != markdown:
        problems.append("generated: requirement-status.md is missing or stale")
    return problems


def main() -> int:
    parser = ArgumentParser(description=__doc__)
    parser.add_argument(
        "--write-generated", action="store_true",
        help="write deterministic generated requirement-status projections",
    )
    args = parser.parse_args()
    if args.write_generated:
        payload, markdown = generated_status()
        GENERATED_DIR.mkdir(parents=True, exist_ok=True)
        STATUS_JSON.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        STATUS_MD.write_text(markdown, encoding="utf-8")
        return 0
    problems = validate() + generated_problems()
    for problem in problems:
        print(problem)
    if problems:
        print(f"\n{len(problems)} evidence violation(s)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
