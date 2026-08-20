"""Validate documentation authority labels, ADRs, inventories, and freshness."""
from __future__ import annotations

import re

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
NUMERIC_ADR = re.compile(r"^ADR-\d{3}-[^/]+\.md$")


def check() -> list[str]:
    problems = []
    for relative in HISTORICAL_DOCUMENTS:
        head = "\n".join((ROOT / relative).read_text(encoding="utf-8").splitlines()[:14]).lower()
        if "superseded" not in head and "historical snapshot" not in head:
            problems.append(f"{relative}: missing historical/superseded label")
    for relative, _summary in FOCUSED_CONTRACTS:
        if not (ROOT / relative).is_file():
            problems.append(f"missing focused contract: {relative}")
    date_names = []
    for path in sorted(CANONICAL_ADR.glob("*.md")):
        if path.name == "README.md":
            continue
        if NUMERIC_ADR.fullmatch(path.name):
            continue
        if not DATE_ADR.fullmatch(path.name):
            problems.append(f"docs/adr/{path.name}: invalid new ADR filename")
        else:
            date_names.append(path.name)
    if len(date_names) != len(set(date_names)):
        problems.append("canonical ADR date-prefixed identifiers are not unique")
    for path in HISTORICAL_ADR.glob("ADR-*.md"):
        if not NUMERIC_ADR.fullmatch(path.name):
            problems.append(f"docs/architecture/adr/{path.name}: historical ADR must be numeric")
    for path, content in expected().items():
        if not path.is_file() or path.read_text(encoding="utf-8") != content:
            problems.append(f"generated documentation is stale: {path.relative_to(ROOT).as_posix()}")
    return problems


if __name__ == "__main__":
    problems = check()
    for problem in problems:
        print(problem)
    raise SystemExit(int(bool(problems)))
