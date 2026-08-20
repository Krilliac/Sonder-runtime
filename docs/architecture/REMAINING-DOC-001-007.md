# REMAINING-DOC-001-007 — Documentation authority closure

This record implements the documentation authority/catalog foundation for
DOC-001 through DOC-005. It does not edit the master-spec checkboxes or the
append-only evidence ledger. DOC-006 and DOC-007 remain process/source-review
work beyond this focused slice.

## Contract coverage

| ID | Contract | Current evidence | Classification |
|---|---|---|---|
| DOC-001 | Authority index | `README.md`, `DOCUMENT-AUTHORITY-INDEX.md` | implemented foundation |
| DOC-002 | Historical labeling | authority index historical table and README labels | implemented foundation |
| DOC-003 | Unique ADR namespace | `adr/README.md` and namespace test | implemented foundation |
| DOC-004 | Focused contracts | focused current-contract map | implemented foundation |
| DOC-005 | Generated references | generated runtime reference, architecture map, and freshness checker | implemented foundation where runtime metadata permits |
| DOC-006 | Status evidence discipline | explicit no-checkbox/no-ledger-change rule and evidence hierarchy | process documented; formal verification remains open |
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
metadata, typed event payload fields, and typed configuration dataclass fields
into deterministic JSON/Markdown references. `GeneratedCatalogs.generate`
remains the application-level client/schema projection; this documentation
adapter does not alter runtime behavior. Unavailable optional metadata is
recorded as an explicit source error. The freshness gate is
`scripts/check_documentation_authority.py` and its focused contract is
`tests/test_remaining_doc_001_005.py`.

## Verification boundary

The focused test is `tests/test_document_authority.py`. It verifies path
existence, classifications, the unique new-ADR naming rule, the focused map,
and generated-catalog references. The test does not mark formal requirements
complete, mutate documentation, or rewrite historical documents.

Formal DOC-001 through DOC-007 checkboxes remain unchecked by design in this
change.
