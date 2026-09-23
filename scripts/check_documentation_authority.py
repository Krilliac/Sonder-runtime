"""Validate documentation authority labels, ADRs, inventories, and freshness."""
from __future__ import annotations

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
    def records(directory):
        return {
            path.name for path in directory.iterdir()
            if path.is_file() and path.name != "README.md"
            and (path.suffix.lower() == ".md" or path.name.startswith("ADR-"))
        }

    canonical_names = records(CANONICAL_ADR)
    historical_names = records(HISTORICAL_ADR)
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
    for path, content in expected().items():
        if not path.is_file() or path.read_text(encoding="utf-8") != content:
            problems.append(f"generated documentation is stale: {path.relative_to(ROOT).as_posix()}")
    return problems


if __name__ == "__main__":
    problems = check()
    for problem in problems:
        print(problem)
    raise SystemExit(int(bool(problems)))
