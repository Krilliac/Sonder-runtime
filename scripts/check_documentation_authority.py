"""Validate documentation authority labels, ADRs, inventories, and freshness."""
from __future__ import annotations

import json
import re
from datetime import date

try:
    from scripts.generate_documentation_catalogs import (
        FOCUSED_CONTRACTS, HISTORICAL_DOCUMENTS, ROOT, expected,
    )
except ModuleNotFoundError:  # direct ``python scripts/check_...py`` execution
    from generate_documentation_catalogs import (  # type: ignore[no-redef]
        FOCUSED_CONTRACTS, HISTORICAL_DOCUMENTS, ROOT, expected,
    )

CANONICAL_ADR = ROOT / "docs" / "adr"
HISTORICAL_ADR = ROOT / "docs" / "architecture" / "adr"
DATE_ADR = re.compile(r"^ADR-\d{4}-\d{2}-\d{2}-[a-z0-9][a-z0-9-]*\.md$")
LEGACY_CANONICAL_ADRS = frozenset({
    "ADR-001-inbound-interfaces-layer.md",
    "ADR-002-no-compatibility-policy.md",
    "ADR-003-startup-capabilities.md",
    "ADR-004-transactional-outbox.md",
    "ADR-005-immutable-training-deployment.md",
    "ADR-006-schema-epoch-2.md",
})
LEGACY_ARCHITECTURE_ADRS = frozenset({
    "ADR-001-modular-monolith.md",
    "ADR-002-ollama-external.md",
    "ADR-003-sqlite-per-domain.md",
    "ADR-004-ports-and-adapters.md",
    "ADR-005-operation-context.md",
    "ADR-006-no-orm.md",
    "ADR-007-compat-shims.md",
    "ADR-008-local-events.md",
    "ADR-009-local-observability.md",
})


def _check_adr_namespace() -> list[str]:
    """Freeze historical numeric IDs and require valid dated IDs for new ADRs."""
    problems = []
    def records(directory, relative):
        names = set()
        for path in directory.iterdir():
            if path.is_symlink():
                problems.append(f"{relative}/{path.name}: ADR namespace symlinks are not permitted")
            elif path.is_dir():
                problems.append(f"{relative}/{path.name}: nested ADR directories are not permitted")
            elif path.is_file() and path.name != "README.md" and (
                path.suffix.lower() == ".md" or path.name.startswith("ADR-")
            ):
                names.add(path.name)
        return names

    canonical_names = records(CANONICAL_ADR, "docs/adr")
    historical_names = records(HISTORICAL_ADR, "docs/architecture/adr")
    for missing in sorted(LEGACY_CANONICAL_ADRS - canonical_names):
        problems.append(f"docs/adr/{missing}: historical ADR is missing")
    for missing in sorted(LEGACY_ARCHITECTURE_ADRS - historical_names):
        problems.append(f"docs/architecture/adr/{missing}: historical ADR is missing")
    for name in sorted(canonical_names - LEGACY_CANONICAL_ADRS):
        if not DATE_ADR.fullmatch(name):
            problems.append(f"docs/adr/{name}: new ADR needs a date-prefixed ID")
            continue
        try:
            date.fromisoformat(name[4:14])
        except ValueError:
            problems.append(f"docs/adr/{name}: invalid ADR date")
    for name in sorted(historical_names - LEGACY_ARCHITECTURE_ADRS):
        problems.append(f"docs/architecture/adr/{name}: historical ADR directory is frozen")
    return problems


MASTER_SPEC = ROOT / "docs" / "architecture" / "SONDER-MASTER-IMPLEMENTATION-SPEC.md"
LEDGER = ROOT / "docs" / "architecture" / "evidence" / "requirements.jsonl"
# DOC-007: product documentation is the root README plus the six focused
# current contracts named by DOC-004.
PRODUCT_DOCUMENTS = ("README.md",) + tuple(path for path, _summary in FOCUSED_CONTRACTS)
STATUS_HEADING = "## Behavior status"
STATUS_LABELS = ("Implemented", "Experimental", "Proposed", "Degraded", "Unsupported")
SPEC_LINK_WINDOW = 12
REQUIREMENT_ID = re.compile(r"\b([A-Z][A-Z0-9]+-[0-9]{3})\b")
SPEC_CHECKBOX = re.compile(r"^\s*- \[(?P<checked>[ xX])\].*?\b(?P<id>[A-Z][A-Z0-9]+-[0-9]{3})\b")
MARKDOWN_LINK = re.compile(r"\]\(([^)\s#]+)(?:#[^)]*)?\)")
# Forward-looking promises must live in a labeled status row, never in prose.
STALE_PROMISE = re.compile(
    r"(?i)\b(?:coming soon|will (?:be )?(?:added|supported|implemented|shipped)"
    r"|will (?:add|support|implement|ship)|in a future|future (?:backend|release|version)"
    r"|not yet (?:available|implemented|supported)|roadmap|TODO|TBD)\b"
)
SLICE_LOG_LINE = re.compile(r"^\s*(?:[-*#]+\s*)?WP\d+ [\w-]+ Slice\b")


def _spec_state() -> dict[str, bool]:
    states = {}
    for line in MASTER_SPEC.read_text(encoding="utf-8").splitlines():
        match = SPEC_CHECKBOX.match(line)
        if match:
            states[match.group("id")] = match.group("checked").lower() == "x"
    return states


def _latest_ledger_status() -> dict[str, str]:
    latest: dict[str, tuple[int, str]] = {}
    for raw in LEDGER.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        requirement_id, revision = record.get("requirement_id"), record.get("revision")
        if isinstance(requirement_id, str) and isinstance(revision, int):
            if requirement_id not in latest or revision > latest[requirement_id][0]:
                latest[requirement_id] = (revision, str(record.get("status")))
    return {key: status for key, (_revision, status) in latest.items()}


def _status_rows(relative: str, lines: list[str], problems: list[str]) -> tuple[int, int, list[list[str]]]:
    """Return the status section span and its table rows."""
    starts = [index for index, line in enumerate(lines) if line.strip() == STATUS_HEADING]
    if len(starts) != 1:
        problems.append(f"{relative}: needs exactly one '{STATUS_HEADING}' section")
        return -1, -1, []
    start = starts[0]
    end = next(
        (index for index in range(start + 1, len(lines)) if lines[index].startswith("## ")),
        len(lines),
    )
    rows = []
    for line in lines[start + 1:end]:
        stripped = line.strip()
        if not stripped.startswith("|") or set(stripped) <= set("|-: "):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if cells and cells[0] == "Behavior":
            continue
        rows.append(cells)
    if not rows:
        problems.append(f"{relative}: '{STATUS_HEADING}' has no status rows")
    return start, end, rows


def _check_product_documents() -> list[str]:
    """DOC-004/DOC-007: focused, labeled, and linked product documentation."""
    problems: list[str] = []
    spec_state = _spec_state()
    ledger_status = _latest_ledger_status()
    labels_used: set[str] = set()
    focused = {path for path, _summary in FOCUSED_CONTRACTS}
    for relative in PRODUCT_DOCUMENTS:
        path = ROOT / relative
        if not path.is_file():
            problems.append(f"{relative}: product document is missing")
            continue
        lines = path.read_text(encoding="utf-8").splitlines()
        if relative in focused:
            linked = any(
                (path.parent / target).resolve() == MASTER_SPEC.resolve()
                for line in lines[:SPEC_LINK_WINDOW]
                for target in MARKDOWN_LINK.findall(line)
            )
            if not linked:
                problems.append(
                    f"{relative}: focused contract must link the master specification "
                    f"within its first {SPEC_LINK_WINDOW} lines"
                )
        start, end, rows = _status_rows(relative, lines, problems)
        for cells in rows:
            if len(cells) != 3:
                problems.append(f"{relative}: status row needs Behavior | Status | Boundary: {cells}")
                continue
            behavior, label, boundary = cells
            if label not in STATUS_LABELS:
                problems.append(f"{relative}: unknown status label {label!r} for {behavior!r}")
                continue
            labels_used.add(label)
            cited = REQUIREMENT_ID.findall(boundary)
            for requirement_id in cited:
                if requirement_id not in spec_state:
                    problems.append(f"{relative}: {behavior!r} cites unknown requirement {requirement_id}")
            if label == "Proposed":
                open_ids = [
                    requirement_id for requirement_id in cited
                    if spec_state.get(requirement_id) is False
                    and ledger_status.get(requirement_id) != "verified"
                ]
                if not open_ids:
                    problems.append(
                        f"{relative}: proposed behavior {behavior!r} must cite an open "
                        "master-spec requirement; update the row if its requirement is complete"
                    )
        for number, line in enumerate(lines, 1):
            if start <= number - 1 < end:
                continue
            if SLICE_LOG_LINE.match(line):
                problems.append(f"{relative}:{number}: implementation slice log belongs in historical records")
            elif match := STALE_PROMISE.search(line):
                problems.append(
                    f"{relative}:{number}: unlabeled forward-looking promise {match.group(0)!r}; "
                    f"state it as a '{STATUS_HEADING}' row"
                )
    for label in STATUS_LABELS:
        if label not in labels_used:
            problems.append(f"product documentation never uses the {label!r} status label")
    return problems


def check() -> list[str]:
    problems = []
    for relative in HISTORICAL_DOCUMENTS:
        head = "\n".join((ROOT / relative).read_text(encoding="utf-8").splitlines()[:14]).lower()
        if "superseded" not in head and "historical snapshot" not in head:
            problems.append(f"{relative}: missing historical/superseded label")
    for relative, _summary in FOCUSED_CONTRACTS:
        if not (ROOT / relative).is_file():
            problems.append(f"missing focused contract: {relative}")
    problems.extend(_check_adr_namespace())
    problems.extend(_check_product_documents())
    for path, content in expected().items():
        if not path.is_file() or path.read_text(encoding="utf-8") != content:
            problems.append(f"generated documentation is stale: {path.relative_to(ROOT).as_posix()}")
    return problems


if __name__ == "__main__":
    problems = check()
    for problem in problems:
        print(problem)
    raise SystemExit(int(bool(problems)))
