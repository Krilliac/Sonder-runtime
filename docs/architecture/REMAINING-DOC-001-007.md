# REMAINING-DOC-001-007 — Documentation authority closure

This record tracks the documentation authority/catalog foundation for
DOC-001 through DOC-004 and the verified generated-reference contract for
DOC-005. DOC-006 has a separate verified evidence slice; DOC-007 remains
process/source-review work beyond this focused slice.

## Contract coverage

| ID | Contract | Current evidence | Classification |
|---|---|---|---|
| DOC-001 | Authority index | `README.md`, `DOCUMENT-AUTHORITY-INDEX.md` | implemented foundation |
| DOC-002 | Historical labeling | authority index historical table and README labels | implemented foundation |
| DOC-003 | Unique ADR namespace | `adr/README.md` and namespace test | implemented foundation |
| DOC-004 | Focused contracts | focused current-contract map | implemented foundation |
| DOC-005 | Generated references | generated runtime reference covers tools, commands, events, configuration, MCP/OpenAI/client/event schemas, and SDK/operational capabilities with source hashes and a direct freshness gate | verified repository reference and freshness contract |
| DOC-006 | Status evidence discipline | explicit evidence coupling, ledger revision, generated status, and CI base-diff gate | verified repository evidence wiring; semantic and privileged-bypass limits remain |
| DOC-007 | No stale promises | inventory below and authority tests | implemented inventory; source prose still requires ordinary review |

## Stale-promise inventory

| Category | Meaning | Examples in this tree | Required reading |
|---|---|---|---|
| Current | Describes shipped/current behavior | focused contracts, typed catalog foundation | current contract plus tests |
| Implemented foundation | A bounded slice exists, but end-state integration may remain | `REMAINING-*.md`, WP work-package evidence | slice evidence and master spec |
| Planned/open | Explicitly not complete | unchecked master-spec items, configuration generation gap | master spec |
| Historical | Preserves an earlier decision, plan, or status | SPEC-5, migration runbook, old ADR series, WP1 slice notes | authority index and latest evidence |
| Limitation | Truthful boundary or unsupported case | “not generated”, provider/network exclusions, platform skips | cited contract and tests |

The following phrases are not completion evidence by themselves: “planned”,
“proposed”, “foundation”, “compatible”, “preserved”, “future”, “should”, and
“remaining”. They must be interpreted using the classification of their source
document and the master-spec checkbox/evidence state.

## Generated-reference freshness

`scripts/generate_documentation_catalogs.py` projects command and MCP tool
metadata, typed event payload fields, typed configuration dataclass fields,
four typed schema projections, and typed SDK/operational capability references
into deterministic JSON/Markdown references. The generated projections share
the `GeneratedCatalogs` digest and include source hashes. The documentation
adapter does not alter runtime behavior and now fails closed when the runtime
tool source cannot be imported. The freshness gate is
`scripts/check_documentation_authority.py`, runs directly in CI, and is also
covered by `tests/test_remaining_doc_001_005.py`.

## Verification boundary

The focused test is `tests/test_document_authority.py`. It verifies path
existence, classifications, the unique new-ADR naming rule, the focused map,
generated-catalog references, and the current DOC-006/DOC-007 checkbox state.
The test does not judge semantic evidence quality or privileged repository
administrative actions.

DOC-001 through DOC-004 and DOC-007 remain unchecked. DOC-005 and DOC-006 are
checked only with their linked verified ledger revisions and evidence documents.
