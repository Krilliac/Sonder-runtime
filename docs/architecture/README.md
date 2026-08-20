# Sonder Runtime architecture documents

## Authoritative implementation plan

- [`SONDER-MASTER-IMPLEMENTATION-SPEC.md`](SONDER-MASTER-IMPLEMENTATION-SPEC.md)
  is the single source of truth for unfinished architecture and implementation work.

## Current preparation artifacts

- [`WP0-BASELINE.md`](WP0-BASELINE.md) and
  [`wp0-baseline.json`](wp0-baseline.json) — commit-qualified read-only baseline.
- [`EVIDENCE-TRACKING-DESIGN.md`](EVIDENCE-TRACKING-DESIGN.md) — proposed requirement
  evidence ledger and CI discipline.
- [`evidence/requirements.jsonl`](evidence/requirements.jsonl) and
  [`evidence/requirement-evidence.schema.json`](evidence/requirement-evidence.schema.json)
  — append-only requirement status and its strict schema.
- [`WP1-FIRST-SLICE.md`](WP1-FIRST-SLICE.md) — first migration slice and verification
  record.
- [`WP1-SECOND-SLICE.md`](WP1-SECOND-SLICE.md) — memory MMR adapter migration and
  verification record.

## Current focused contracts

- [`../../ARCHITECTURE.md`](../../ARCHITECTURE.md) — product boundary and current runtime.
- [`../../SECURITY.md`](../../SECURITY.md) — current security contract.
- [`../../SELFMOD.md`](../../SELFMOD.md) — current self-modification lifecycle.
- [`../../TRAINING.md`](../../TRAINING.md) — current training lifecycle.
- [`../../CLIENT.md`](../../CLIENT.md) — current client contract.
- [`../../MOBILE_HOST_CONTROL.md`](../../MOBILE_HOST_CONTROL.md) — remote host control.
- [`external-mcp-bridge.md`](external-mcp-bridge.md) — guarded external MCP behavior.
- [`queued-action-lifecycle.md`](queued-action-lifecycle.md) — queued action foundation.
- [`refinement-transactions.md`](refinement-transactions.md) — refinement transactions.
- [`tool-capability-registry.md`](tool-capability-registry.md) — capability registry phase.

## Historical/superseded program documents

These preserve decisions and implementation history. They do not define current status
or authorize a competing implementation path:

- [`SPEC-5-End-State-Architecture.md`](SPEC-5-End-State-Architecture.md)
- [`SPEC-5-MIGRATION-RUNBOOK.md`](SPEC-5-MIGRATION-RUNBOOK.md)
- [`PROGRAM-STATUS.md`](PROGRAM-STATUS.md)

## ADRs

Two historical ADR series exist and contain overlapping numbers:

- `docs/adr/` records SPEC-5-era product decisions.
- `docs/architecture/adr/` records the preceding architecture-program decisions.

New decisions must use a globally unique date-prefixed filename under `docs/adr/`, for
example `ADR-2026-08-19-event-sourced-sessions.md`. Existing ADRs retain their paths so
old links remain valid. Their decisions remain authoritative unless the master spec or a
newer ADR explicitly supersedes them.

## Status discipline

- A proposal, plan, class, test double, or historical status statement is not proof that
  a master-spec checkbox is complete.
- Checkboxes are changed only with linked committed evidence.
- Focused current-behavior documentation must not claim planned behavior is implemented.
