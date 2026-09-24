# Document authority index

This index defines which documents describe current behavior, which preserve
history, and which are generated evidence. It is deliberately narrower than a
file listing: a document is authoritative only for the scope named here.

## Authority order

1. `SONDER-MASTER-IMPLEMENTATION-SPEC.md` is authoritative for the unfinished
   requirement list and checkbox state. A checkbox changes only in the same
   change that adds its verified ledger revision and evidence (DOC-006).
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

Each of the six root focused contracts (`ARCHITECTURE.md` through
`MOBILE_HOST_CONTROL.md`) links the master specification within its first
twelve lines and ends its current-behavior description with one
`## Behavior status` table. Unfinished work appears there only as a
`Proposed` row that cites an open master-spec requirement ID, so the
specification remains the single home of unfinished implementation work.

## Documentation status vocabulary

Product documentation is the root `README.md` plus the six focused contracts
above. Each product document carries exactly one `## Behavior status` table
with the columns `Behavior | Status | Boundary`. The status cell uses exactly
one of these labels:

| Label | Meaning |
|---|---|
| Implemented | Current behavior in this repository, subject to the boundary stated in the row and the surrounding contract. |
| Experimental | Shipped but opt-in, prerelease, lab-only, or default-off; interfaces and results may change and are not release-qualified. |
| Proposed | End-state behavior required by the master specification that is not a current contract. The row must cite at least one unchecked, unverified master-spec requirement; partial foundations may exist but must not be relied on. |
| Degraded | Available with a documented reduction: it fails closed, falls back, or is less reliable in a named configuration. |
| Unsupported | Deliberately not provided; the request is rejected, the capability is absent, or the configuration is outside the supported deployment. |

`scripts/check_documentation_authority.py` enforces the vocabulary: unknown
labels, unknown requirement IDs, and `Proposed` rows whose cited requirements
are all checked or verified fail the gate, so completing a requirement forces
the corresponding promise to be restated as current behavior. Forward-looking
phrases such as “coming soon”, “in a future”, “future backend”, or “not yet
available” are rejected outside the status table, and WP implementation slice
logs are rejected in product documentation; those belong in historical
records such as [`WP1-README-SLICE-LOG.md`](WP1-README-SLICE-LOG.md).

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
| `WP1-README-SLICE-LOG.md` | implementation history | WP1 slice notes relocated verbatim from the root README |

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
| MCP/OpenAI/event schemas | typed `GeneratedCatalogs` projections | shared catalog digest plus generated-reference freshness check | implemented foundation |
| Capabilities | typed `CapabilitySnapshot` and operational capability projection | capability catalog digest, source hashes, and generated-reference freshness check | implemented foundation |

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

DOC-001 through DOC-003 are checked with separate verified authority,
historical-label, and ADR-namespace evidence. DOC-005 is checked with
generated references for all six named families, a direct CI freshness gate,
and linked exact-head and post-merge evidence. DOC-006 is checked with its
separate verified ledger revision. DOC-004 is checked with the focused-contract
linkage and status-table gate, and DOC-007 with the product-document status
vocabulary gate; both link separate evidence records.
