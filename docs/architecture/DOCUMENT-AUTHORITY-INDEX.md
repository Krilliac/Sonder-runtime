# Document authority index

This index defines which documents describe current behavior, which preserve
history, and which are generated evidence. It is deliberately narrower than a
file listing: a document is authoritative only for the scope named here.

## Authority order

1. `SONDER-MASTER-IMPLEMENTATION-SPEC.md` is authoritative for the unfinished
   requirement list and checkbox state. This pass intentionally leaves all
   formal checkboxes unchanged.
2. The focused contract documents below are authoritative for current product
   boundaries, subject to the master specification where the two conflict.
3. Requirement evidence and generated status projections are authoritative for
   what has been verified, not for what is merely planned.
4. Architecture slices, remaining-work notes, SPEC-5 documents, runbooks, and
   status snapshots are historical or planning evidence unless explicitly
   linked as a current contract.

## Focused current-contract map

| Scope | Authoritative document | What it owns |
|---|---|---|
| Architecture | [`../../ARCHITECTURE.md`](../../ARCHITECTURE.md) | Product/package boundaries |
| Security | [`../../SECURITY.md`](../../SECURITY.md) | Security and credential policy |
| Self-modification | [`../../SELFMOD.md`](../../SELFMOD.md) | Proposal, review, and activation lifecycle |
| Training | [`../../TRAINING.md`](../../TRAINING.md) | Dataset, evaluation, and deployment contract |
| Client | [`../../CLIENT.md`](../../CLIENT.md) | Client-facing behavior and compatibility |
| Mobile host control | [`../../MOBILE_HOST_CONTROL.md`](../../MOBILE_HOST_CONTROL.md) | Remote host-control boundary |
| External MCP | [`external-mcp-bridge.md`](external-mcp-bridge.md) | Guarded external MCP behavior |
| Queued actions | [`queued-action-lifecycle.md`](queued-action-lifecycle.md) | Queue lifecycle foundation |
| Refinement | [`refinement-transactions.md`](refinement-transactions.md) | Refinement transaction boundary |
| Tools | [`tool-capability-registry.md`](tool-capability-registry.md) | Typed tool capability source |

The focused documents describe current contracts, but a sentence using
“planned”, “proposed”, “future”, or “not implemented” is not evidence that its
corresponding master-spec requirement is complete.

## Historical and superseded documents

The following are intentionally retained for traceability and are labeled here
so their imperative language cannot be mistaken for current authority:

| Path | Classification | Use |
|---|---|---|
| `SPEC-5-End-State-Architecture.md` | historical/superseded | Earlier end-state design |
| `SPEC-5-MIGRATION-RUNBOOK.md` | historical/runbook | Earlier migration procedure |
| `PROGRAM-STATUS.md` | historical snapshot | Earlier program status |
| `WP0-BASELINE.md`, `wp0-baseline.json` | historical baseline | Baseline evidence |
| `WP1-*.md` through `WP9-*.md` | implementation history | Slice/work-package evidence |
| `REMAINING-*.md` | planning/contract evidence | Partial or isolated follow-up slices |
| `REQUIREMENT-AUDIT-NEXT.md` | audit snapshot | Requirement audit, not completion proof |

Historical documents must not be used to infer current status without checking
the master specification and the latest evidence record.

## ADR namespace

`docs/adr/` is the canonical directory for new ADRs. New files must use
`ADR-YYYY-MM-DD-<slug>.md`; the date plus slug is the globally unique ADR
identifier. `docs/architecture/adr/` is a historical directory containing the
numbered architecture-program ADRs. The older numeric series in `docs/adr/`
is also retained as historical SPEC-5-era material. See
[`adr/README.md`](adr/README.md) for the collision and supersession rules.

## Generated-reference contract

The generated catalog foundation is
`sonder_runtime/application/tools/generated_catalogs.py`. It derives bounded,
deterministic MCP, OpenAI, CLI, and client projections from typed tool/event
sources and emits a SHA-256 freshness digest. The contract is exercised by
`tests/test_remaining_tool_catalogs.py` and by the freshness assertions in
`tests/test_document_authority.py`.

| Reference family | Current source | Freshness evidence | State |
|---|---|---|---|
| Tools | typed tool registry | catalog digest and deterministic regeneration | implemented foundation |
| Commands | typed command inputs to `GeneratedCatalogs.generate` | catalog digest and bounds | implemented foundation |
| Events | `EventKind` and payload schemas | catalog digest and derived event schema | implemented foundation |
| Client schema | generated client projection | client digest equals bundle digest | implemented foundation |
| Configuration | typed configuration dataclasses | generated field/default projection and source digest | generated where metadata permits |

A changed typed source must produce a changed digest; a reordered source must
not. Oversized output fails closed rather than silently truncating.

## Status and stale-promise discipline

The stale-promise inventory is maintained in
[`REMAINING-DOC-001-007.md`](REMAINING-DOC-001-007.md). It distinguishes
implemented foundations, documented limitations, planned work, and historical
claims. A document-authority test verifies that the inventory names the
required categories and that the focused paths and generated catalog source
still exist.

## Formal requirement status

DOC-001 through DOC-007 remain unchecked in the master specification after this
documentation change. The requested documentation contracts are present and
tested, but no formal checkbox or evidence-ledger record is changed here.
