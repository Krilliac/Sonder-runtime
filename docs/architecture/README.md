# Sonder Runtime architecture documents

> Authority note: this page is the human-facing entry point. The complete
> authority map, document classifications, generated-reference contract, and
> stale-promise inventory live in
> [`DOCUMENT-AUTHORITY-INDEX.md`](DOCUMENT-AUTHORITY-INDEX.md). The master
> specification remains the authoritative list of unfinished requirements;
> this documentation pass does not alter its checkboxes.

## Read this first

- **Unfinished requirements:**
  [`SONDER-MASTER-IMPLEMENTATION-SPEC.md`](SONDER-MASTER-IMPLEMENTATION-SPEC.md)
- **Current focused contracts:** see the map in
  [`DOCUMENT-AUTHORITY-INDEX.md`](DOCUMENT-AUTHORITY-INDEX.md).
- **Historical program material:** SPEC-5, its migration runbook, and the
  program-status snapshot are explicitly historical; they are not current
  implementation authority.
- **New ADRs:** use the single canonical namespace under `docs/adr/` with a
  globally unique date-prefixed filename. The numbered files under
  `docs/architecture/adr/` are retained as historical records; see
  [`adr/README.md`](adr/README.md).
- **Generated references:** tool, command, event, and client projections are
  generated from typed application/domain sources by
  `GeneratedCatalogs`; freshness is represented by its deterministic SHA-256
  digest and covered by the document-authority tests. The checked-in runtime
  reference is [`generated/runtime-reference.md`](generated/runtime-reference.md);
  its JSON companion is machine-readable. Configuration fields are projected
  from typed configuration dataclasses where runtime metadata permits.

- **Generated maps:** [`generated/architecture-map.md`](generated/architecture-map.md)
  records package layers and composition roots, while
  [`generated/focused-contract-inventory.md`](generated/focused-contract-inventory.md)
  is the complete current-contract inventory. Refresh with
  `python scripts/generate_documentation_catalogs.py --write` and gate with
  `python scripts/check_documentation_authority.py`.

- WP1 Two-Hundred-Forty-Ninth Slice: `WP1-TWO-HUNDRED-FORTY-NINTH-SLICE.md` —
  moved pure launcher output, timeout, and retention policy into a packaged
  adapter while preserving root helper aliases.
- WP1 Two-Hundred-Fiftieth Slice: `WP1-TWO-HUNDRED-FIFTIETH-SLICE.md` —
  moved read-only memory-quality doctor policy into the packaged bootstrap
  boundary while preserving the root compatibility delegate.

- WP1 Two-Hundred-Forty-Fourth Slice: `WP1-TWO-HUNDRED-FORTY-FOURTH-SLICE.md` —
  moved launcher idempotency-key and durable replay validation into a packaged
  adapter while preserving root helper and regex aliases.

- WP1 Two-Hundred-Forty-Eighth Slice: `WP1-TWO-HUNDRED-FORTY-EIGHTH-SLICE.md` —
  moved standalone-client endpoint and fallback orchestration into packaged
  adapters while preserving root compatibility aliases.

- WP1 Two-Hundred-Forty-Fifth Slice: `WP1-TWO-HUNDRED-FORTY-FIFTH-SLICE.md` —
  moved root hardware probe classification to the packaged platform identity
  boundary while preserving the root compatibility aliases.

- WP1 Two-Hundred-Thirty-Ninth Slice: `WP1-TWO-HUNDRED-THIRTY-NINTH-SLICE.md` —
  moved standalone-client HTTP execution into the packaged transport adapter
  while preserving the root request-builder compatibility seam.

- WP1 Two-Hundred-Thirty-Fifth Slice: `WP1-TWO-HUNDRED-THIRTY-FIFTH-SLICE.md` —
  moved speculative-tool safety policy into the packaged domain boundary while
  preserving the root allowlist alias.

- WP1 Two-Hundred-Fortieth Slice: `WP1-TWO-HUNDRED-FORTIETH-SLICE.md` —
  moved pure doctor terminal formatting and status rollup into the packaged
  bootstrap boundary while preserving root compatibility aliases.

- WP1 Two-Hundred-Forty-First Slice: `WP1-TWO-HUNDRED-FORTY-FIRST-SLICE.md` —
  moved the pure thinking-budget exhaustion predicate into the packaged
  domain boundary while preserving the root `server` compatibility alias.

- WP1 Two-Hundred-Twenty-Seventh Slice: `WP1-TWO-HUNDRED-TWENTY-SEVENTH-SLICE.md` —
  moved environment-file parsing into the packaged configuration policy
  boundary while preserving the root `ConfigError` compatibility contract.

- WP1 One-Hundred-Eighty-Seventh Slice: `WP1-ONE-HUNDRED-EIGHTY-SEVENTH-SLICE.md` —
  moved pure learning-enablement policy into the packaged domain boundary
  while preserving the root server compatibility wrapper.

## Authoritative implementation plan

- [`SONDER-MASTER-IMPLEMENTATION-SPEC.md`](SONDER-MASTER-IMPLEMENTATION-SPEC.md)
  is the single source of truth for unfinished architecture and implementation work.

## Current preparation artifacts

- WP1 One-Hundred-Seventy-Seventh Slice: `WP1-ONE-HUNDRED-SEVENTY-SEVENTH-SLICE.md` —
  moved pure deployment-authentication policy into the packaged platform
  boundary while preserving the root server compatibility delegate.

- [`WP0-BASELINE.md`](WP0-BASELINE.md) and
  [`wp0-baseline.json`](wp0-baseline.json) — commit-qualified read-only baseline.
- [`EVIDENCE-TRACKING-DESIGN.md`](EVIDENCE-TRACKING-DESIGN.md) — proposed requirement
  evidence ledger and CI discipline.
- [`evidence/requirements.jsonl`](evidence/requirements.jsonl) and
  [`evidence/requirement-evidence.schema.json`](evidence/requirement-evidence.schema.json)
  — append-only requirement status and its strict schema.
- [`generated/requirement-status.md`](generated/requirement-status.md) and
  [`generated/requirement-status.json`](generated/requirement-status.json) — deterministic
  status projections checked for freshness in CI.
- [`WP1-FIRST-SLICE.md`](WP1-FIRST-SLICE.md) — first migration slice and verification
  record.
- [`WP1-SECOND-SLICE.md`](WP1-SECOND-SLICE.md) — memory MMR adapter migration and
  verification record.
- [`WP1-THIRD-SLICE.md`](WP1-THIRD-SLICE.md) — root reward facade retirement and
  verification record.
- [`WP1-FOURTH-SLICE.md`](WP1-FOURTH-SLICE.md) — process-liveness alias retirement
  and verification record.
- [`WP1-FIFTH-SLICE.md`](WP1-FIFTH-SLICE.md) — evaluation-history alias retirement
  and verification record.
- [`WP1-SIXTH-SLICE.md`](WP1-SIXTH-SLICE.md) — semantic-recall alias retirement
  and verification record.
- [`WP1-SEVENTH-SLICE.md`](WP1-SEVENTH-SLICE.md) — backup, preflight, and workflow
  alias retirement and verification record.
- [`WP1-EIGHTH-SLICE.md`](WP1-EIGHTH-SLICE.md) — storage diagnostics adapter
  retirement and verification record.
- [`WP1-NINTH-SLICE.md`](WP1-NINTH-SLICE.md) — model transport error adapter
  retirement and verification record.
- [`WP1-TENTH-SLICE.md`](WP1-TENTH-SLICE.md) — runtime policy adapter migration
  and verification record.
- [`WP1-ELEVENTH-SLICE.md`](WP1-ELEVENTH-SLICE.md) — Ollama endpoint adapter
  migration and verification record.
- [`WP1-TWELFTH-SLICE.md`](WP1-TWELFTH-SLICE.md) — embedding cache adapter
  migration and verification record.
- [`WP1-ONE-HUNDRED-THIRTEENTH-SLICE.md`](WP1-ONE-HUNDRED-THIRTEENTH-SLICE.md) —
  packaged autopilot repository ownership and compatibility boundary.
- [`WP1-ONE-HUNDRED-FOURTEENTH-SLICE.md`](WP1-ONE-HUNDRED-FOURTEENTH-SLICE.md) —
  packaged HTTP serve-temperature policy and compatibility boundary.
- [`WP1-ONE-HUNDRED-FIFTEENTH-SLICE.md`](WP1-ONE-HUNDRED-FIFTEENTH-SLICE.md) —
  packaged model capability normalization and compatibility boundary.
- [`WP1-ONE-HUNDRED-EIGHTEENTH-SLICE.md`](WP1-ONE-HUNDRED-EIGHTEENTH-SLICE.md) —
  packaged inline-thinking policy and compatibility boundary.
- [`WP1-THIRTEENTH-SLICE.md`](WP1-THIRTEENTH-SLICE.md) — embedding adapter
  migration and remaining NPU boundary.
- [`WP1-FOURTEENTH-SLICE.md`](WP1-FOURTEENTH-SLICE.md) — NPU contract adapter
  migration and verification record.
- [`WP1-FIFTEENTH-SLICE.md`](WP1-FIFTEENTH-SLICE.md) — NPU manifest/provider
  adapter migration and verification record.
- [`WP1-SIXTEENTH-SLICE.md`](WP1-SIXTEENTH-SLICE.md) — NPU process-boundary
  migration and packaged-worker verification record.
- [`WP1-SEVENTEENTH-SLICE.md`](WP1-SEVENTEENTH-SLICE.md) — response activity
  tracker migration and verification record.
- [`WP1-EIGHTEENTH-SLICE.md`](WP1-EIGHTEENTH-SLICE.md) — packaged autopilot
  persistence with an immutable-migration compatibility boundary.
- [`WP1-NINETEENTH-SLICE.md`](WP1-NINETEENTH-SLICE.md) — packaged fleet
  persistence with an immutable-migration compatibility boundary.
- [`WP1-TWENTIETH-SLICE.md`](WP1-TWENTIETH-SLICE.md) — packaged operations
  persistence and root implementation retirement.
- [`WP1-TWENTY-FIRST-SLICE.md`](WP1-TWENTY-FIRST-SLICE.md) — packaged guarded
  filesystem operations and caller migration.
- [`WP1-TWENTY-SECOND-SLICE.md`](WP1-TWENTY-SECOND-SLICE.md) — packaged
  workbench execution and live-reload migration.
- [`WP1-TWENTY-THIRD-SLICE.md`](WP1-TWENTY-THIRD-SLICE.md) — packaged secret
  rotation and authentication-secret callers.
- [`WP1-TWENTY-FOURTH-SLICE.md`](WP1-TWENTY-FOURTH-SLICE.md) — packaged signed
  update service and scoped external verification boundaries.
- [`WP1-TWENTY-FIFTH-SLICE.md`](WP1-TWENTY-FIFTH-SLICE.md) — packaged update
  orchestration engine and scoped bootstrap health-check boundary.
- [`WP1-TWENTY-SIXTH-SLICE.md`](WP1-TWENTY-SIXTH-SLICE.md) — packaged HTTP
  lifecycle and admission boundary.
- [`WP1-TWENTY-SEVENTH-SLICE.md`](WP1-TWENTY-SEVENTH-SLICE.md) — packaged HTTP
  serving interface and exact legacy-backed composition boundary.
- [`WP1-TWENTY-EIGHTH-SLICE.md`](WP1-TWENTY-EIGHTH-SLICE.md) — packaged REPL
- [`WP1-TWENTY-NINTH-SLICE.md`](WP1-TWENTY-NINTH-SLICE.md) — packaged migration
  registry and cycle-free persistence boundary
- [`WP1-THIRTIETH-SLICE.md`](WP1-THIRTIETH-SLICE.md) — response formatting
  adapter extraction from the server composition root
- [`WP1-THIRTY-FIRST-SLICE.md`](WP1-THIRTY-FIRST-SLICE.md) — bounded trace
  buffer adapter extraction from the server composition root
- [`WP1-THIRTY-SECOND-SLICE.md`](WP1-THIRTY-SECOND-SLICE.md) — pure command
  parser adapter extraction
- [`WP1-THIRTY-THIRD-SLICE.md`](WP1-THIRTY-THIRD-SLICE.md) — lesson-ID
  validation adapter extraction
- [`WP1-THIRTY-FOURTH-SLICE.md`](WP1-THIRTY-FOURTH-SLICE.md) — account
  rendering adapter extraction
- [`WP1-THIRTY-FIFTH-SLICE.md`](WP1-THIRTY-FIFTH-SLICE.md) — file-result
  formatter consolidation
- [`WP1-THIRTY-SIXTH-SLICE.md`](WP1-THIRTY-SIXTH-SLICE.md) — checklist
  formatting adapter extraction
- [`WP1-THIRTY-SEVENTH-SLICE.md`](WP1-THIRTY-SEVENTH-SLICE.md) — context-health
  formatter extraction
- [`WP1-THIRTY-EIGHTH-SLICE.md`](WP1-THIRTY-EIGHTH-SLICE.md) — activity-status
  formatter extraction
- [`WP1-FORTIETH-SLICE.md`](WP1-FORTIETH-SLICE.md) — packaged command catalog
  and retired-entrypoint manifest boundary
- [`WP1-FORTY-FIRST-SLICE.md`](WP1-FORTY-FIRST-SLICE.md) — canonical system-clock
  boundary removed from strangler services
- [`WP1-FORTY-SECOND-SLICE.md`](WP1-FORTY-SECOND-SLICE.md) — distillation
  reporting formatter adapter extraction from the server composition root
- [`WP1-FIFTY-FIRST-SLICE.md`](WP1-FIFTY-FIRST-SLICE.md) — runtime readiness
- [`WP1-FIFTY-SECOND-SLICE.md`](WP1-FIFTY-SECOND-SLICE.md) — run-result
- [`WP1-FIFTY-FIFTH-SLICE.md`](WP1-FIFTY-FIFTH-SLICE.md) — Ollama endpoint caller boundary
- [`WP1-FIFTY-SIXTH-SLICE.md`](WP1-FIFTY-SIXTH-SLICE.md) — learning-tier model/provider presentation boundary
- [`WP1-FIFTY-SEVENTH-SLICE.md`](WP1-FIFTY-SEVENTH-SLICE.md) — typed immutable
  runtime model/tier configuration projection
- [`WP1-FIFTY-EIGHTH-SLICE.md`](WP1-FIFTY-EIGHTH-SLICE.md) — packaged Ollama
  lifecycle adapter boundary
- [`WP1-FIFTY-NINTH-SLICE.md`](WP1-FIFTY-NINTH-SLICE.md) — typed runtime
  configuration consumed by the live cloud-default compatibility caller
- [`WP1-SIXTIETH-SLICE.md`](WP1-SIXTIETH-SLICE.md) — narrowed the root Ollama
  lifecycle compatibility surface to explicit public helpers
- [`WP1-FIFTY-FOURTH-SLICE.md`](WP1-FIFTY-FOURTH-SLICE.md) — goal presentation
- [`WP1-FORTY-SEVENTH-SLICE.md`](WP1-FORTY-SEVENTH-SLICE.md) — active
  legacy-root policy narrowed to the roots with live package callers
- [`WP1-FIFTIETH-SLICE.md`](WP1-FIFTIETH-SLICE.md) — fleet-store compatibility
  alias reduced to migration-only status
- [`WP1-THIRTY-NINTH-SLICE.md`](WP1-THIRTY-NINTH-SLICE.md) — model-inventory
  presentation adapter extraction
  interface and exact legacy-backed composition boundary.

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

The ADR namespace policy and historical directory inventory are maintained in
[`adr/README.md`](adr/README.md). New decisions use `docs/adr/ADR-YYYY-MM-DD-<slug>.md`;
existing numeric ADRs are historical and are not renumbered in place.

## Status discipline

- WP1 Seventy-Fifth Slice: `WP1-SEVENTY-FIFTH-SLICE.md` — moved the filesystem
  workbench's logging/redaction caller to the canonical platform seam.
- WP1 Eighty-First Slice: `WP1-EIGHTY-FIRST-SLICE.md` — moved filesystem
  operations to the canonical packaged path seam while preserving default-home
  resolution and containment behavior.
- WP1 Seventy-Seventh Slice: `WP1-SEVENTY-SEVENTH-SLICE.md` — moved the local
  observability adapter's logging/redaction caller to the canonical platform
  seam.

- A proposal, plan, class, test double, or historical status statement is not proof that
  a master-spec checkbox is complete.
- Checkboxes are changed only with linked committed evidence.
- Focused current-behavior documentation must not claim planned behavior is implemented.
- WP1 Forty-Fifth Slice: `WP1-FORTY-FIFTH-SLICE.md` — extracted pure chat usage presentation into the observability adapter boundary.
- WP1 Forty-Sixth Slice: `WP1-FORTY-SIXTH-SLICE.md` — extracted pure REPL duration presentation into the observability adapter boundary.
- WP1 Forty-Eighth Slice: `WP1-FORTY-EIGHTH-SLICE.md` — extracted pure HTTP command-completion limit normalization into a package adapter boundary.
- WP1 Fifty-Second Slice: `WP1-FIFTY-SECOND-SLICE.md` — extracted pure run-result presentation into the observability adapter boundary.
- WP1 Sixty-First Slice: `WP1-SIXTY-FIRST-SLICE.md` — moved the preflight adapter's typed configuration import to the packaged platform boundary.
- WP1 Sixty-Fourth Slice: `WP1-SIXTY-FOURTH-SLICE.md` — moved the HTTP API-key policy caller to the packaged configuration boundary.
- WP1 Sixty-Third Slice: `WP1-SIXTY-THIRD-SLICE.md` — moved the packaged entrypoint's typed configuration import to the packaged platform boundary.
- WP1 Sixty-Sixth Slice: `WP1-SIXTY-SIXTH-SLICE.md` — moved the evaluation-history adapter's path caller to the packaged platform boundary.
- WP1 Sixty-Eighth Slice: `WP1-SIXTY-EIGHTH-SLICE.md` — locked the retired evaluation-history root out of local packages and verified the canonical package payload.
- WP1 Sixty-Ninth Slice: `WP1-SIXTY-NINTH-SLICE.md` — moved the runtime-policy adapter's state-file caller to the packaged platform path boundary.
- WP1 Seventieth Slice: `WP1-SEVENTIETH-SLICE.md` — moved the packaged lifecycle shutdown-coordinator caller to the platform boundary.
- WP1 Seventy-First Slice: `WP1-SEVENTY-FIRST-SLICE.md` — moved the packaged preference adapter's default memory database path caller to the platform boundary.
- WP1 Seventy-Second Slice: `WP1-SEVENTY-SECOND-SLICE.md` — moved the packaged lifecycle metrics caller to the platform boundary.
- WP1 Seventy-Fourth Slice: `WP1-SEVENTY-FOURTH-SLICE.md` — moved the packaged backup adapter's build-identity caller to the platform version boundary.
- WP1 Seventy-Eighth Slice: `WP1-SEVENTY-EIGHTH-SLICE.md` — moved the packaged NPU manifest directory caller to the platform path boundary.
- WP1 Seventy-Ninth Slice: `WP1-SEVENTY-NINTH-SLICE.md` — moved the NPU service shadow-ledger state caller to the platform path boundary.
- WP1 Eightieth Slice: `WP1-EIGHTIETH-SLICE.md` — moved the packaged secret-rotation state-path caller to the platform path boundary.
- WP1 Eighty-Second Slice: `WP1-EIGHTY-SECOND-SLICE.md` — moved the update engine's read-only build-identity caller to the platform version boundary without changing signed-update or release metadata behavior.
- WP1 Eighty-Third Slice: `WP1-EIGHTY-THIRD-SLICE.md` — moved workflow state-home resolution to the canonical platform path boundary without changing persistence, containment, or atomic-write semantics.
- WP1 Eighty-Fourth Slice: `WP1-EIGHTY-FOURTH-SLICE.md` — moved the update service's build-identity caller to the platform version boundary while preserving bundle metadata and signed-update verification.
- WP1 Eighty-Fifth Slice: `WP1-EIGHTY-FIFTH-SLICE.md` — moved the autopilot persistence database-path caller to the platform path boundary without changing database or migration semantics.
- WP1 Eighty-Sixth Slice: `WP1-EIGHTY-SIXTH-SLICE.md` — moved fleet database and principal-credential path resolution to the canonical platform path boundary without changing SQLite or migration semantics.
- WP1 Eighty-Eighth Slice: `WP1-EIGHTY-EIGHTH-SLICE.md` — moved operations-store redaction dependencies to the canonical platform logging boundary without changing durable event persistence semantics.
- WP1 Ninetieth Slice: `WP1-NINETIETH-SLICE.md` — moved the strangler unit-of-work's default memory database path caller to the canonical platform path boundary without removing the live memory-store port.
- WP1 Ninety-First Slice: `WP1-NINETY-FIRST-SLICE.md` — moved the filesystem workbench's Bash executable caller to the canonical platform path boundary while preserving workspace resolution and containment semantics.
- WP1 Eighty-Ninth Slice: `WP1-EIGHTY-NINTH-SLICE.md` — moved the migrations adapter's operations-database path caller to the canonical platform path boundary without changing immutable migration replay or database locations.
- WP1 Eighty-Seventh Slice: `WP1-EIGHTY-SEVENTH-SLICE.md` — moved queued-action database path resolution to the canonical platform path boundary without changing queue or immutable migration semantics.
- WP1 Ninety-Second Slice: `WP1-NINETY-SECOND-SLICE.md` — moved the update engine's default release and active-pointer path callers to the canonical platform path boundary without changing signed-update or bootstrap behavior.
- WP1 Ninety-Fourth Slice: `WP1-NINETY-FOURTH-SLICE.md` — moved all remaining migration path and lock callers to the identity-preserving platform path boundary without changing immutable migration replay or database locations.
- WP1 Ninety-Fifth Slice: `WP1-NINETY-FIFTH-SLICE.md` — moved the packaged entrypoint's dynamic default-backup path caller to the canonical platform path boundary while preserving default-home resolution and target precedence.
- WP1 Ninety-Third Slice: `WP1-NINETY-THIRD-SLICE.md` — moved the packaged entrypoint's build-metadata reads to the canonical platform version boundary without changing release metadata or compatibility behavior.
- WP1 Ninety-Sixth Slice: `WP1-NINETY-SIXTH-SLICE.md` — moved the packaged web lifecycle's build-identity caller to the canonical platform version boundary without changing lifecycle metrics or version payload behavior.
- WP1 Ninety-Seventh Slice: `WP1-NINETY-SEVENTH-SLICE.md` — moved `sonder_paths` implementation ownership into the packaged platform boundary while preserving the root compatibility module identity, public symbols, overrides, and legacy database migration behavior.
- WP1 Ninety-Eighth Slice: `WP1-NINETY-EIGHTH-SLICE.md` — moved structured logging and redaction implementation ownership to the packaged platform boundary while preserving the root compatibility module identity.
- WP1 One-Hundredth Slice: `WP1-ONE-HUNDREDTH-SLICE.md` — moved full shutdown implementation ownership to the packaged platform boundary while preserving root import identity and drain semantics.
- WP1 One-Hundred-Fifth Slice: `WP1-ONE-HUNDRED-FIFTH-SLICE.md` — moved full service-state implementation ownership to the packaged platform boundary while preserving root import identity, monkeypatch surfaces, and lifecycle/concurrency semantics.
- WP1 One-Hundred-First Slice: `WP1-ONE-HUNDRED-FIRST-SLICE.md` — moved build-identity implementation ownership to the packaged platform boundary while retaining the root literal version contract and compatibility module identity required by release tooling.
- WP1 One-Hundred-Second Slice: `WP1-ONE-HUNDRED-SECOND-SLICE.md` — moved complete typed configuration ownership to the packaged platform boundary while preserving the root external-tooling surface, exact object identity, and configuration semantics.
- WP1 One-Hundred-Third Slice: `WP1-ONE-HUNDRED-THIRD-SLICE.md` — migrated the persistence migration registry's final build-identity caller to the packaged version boundary while preserving immutable migration bytes, checksums, replay, and release metadata.
- WP1 One-Hundred-Sixth Slice: `WP1-ONE-HUNDRED-SIXTH-SLICE.md` — moved full system-profile implementation ownership to the packaged platform boundary while preserving root import identity, mutable probe state, hardware detection, profile editing, and monkeypatch behavior.
- WP1 One-Hundred-Seventh Slice: `WP1-ONE-HUNDRED-SEVENTH-SLICE.md` — removed the `sonder_version` root-platform allowance after all packaged runtime callers moved to `sonder_runtime.platform.version`, while preserving the literal release-tooling version contract.
- WP1 One-Hundred-Eighth Slice: `WP1-ONE-HUNDRED-EIGHTH-SLICE.md` — extracted pure Ollama-origin security policy into the domain layer so platform configuration and unsafe-lab validation do not depend on the transport adapter.
- WP1 Ninety-Ninth Slice: `WP1-NINETY-NINTH-SLICE.md` — moved metrics implementation ownership to the packaged platform boundary while preserving registry identity, metric names and labels, optional-client behavior, and root compatibility imports.
- WP1 Sixty-Seventh Slice: `WP1-SIXTY-SEVENTH-SLICE.md` — moved the embedding-cache adapter's path caller to the packaged platform boundary.
- WP1 Sixty-Fifth Slice: `WP1-SIXTY-FIFTH-SLICE.md` — moved the read-only doctor configuration caller to the packaged platform boundary.
# WP1 One-Hundred-Tenth-SLICE

- `server._runtime_identity_block` is now owned by the pure domain module
  `sonder_runtime.domain.runtime_identity`; the server retains only the
  compatibility alias and composition wiring.

- WP1 One-Hundred-Eleventh Slice: `WP1-ONE-HUNDRED-ELEVENTH-SLICE.md` — retired
  the duplicate `sonder_metrics.py` root delegate after production and
  regression callers moved to the canonical packaged metrics module.
- WP1 One-Hundred-Sixteenth Slice: `WP1-ONE-HUNDRED-SIXTEENTH-SLICE.md` — moved
  the process-probe port adapter out of generic strangler services into its
  named packaged adapter and wired the composition root directly to it.
- WP1 One-Hundred-Nineteenth Slice: `WP1-ONE-HUNDRED-NINETEENTH-SLICE.md` — moved
  the pure fanout catalog-target eligibility policy into the domain boundary
  while preserving the server compatibility alias.
- WP1 One-Hundred-Twentieth Slice: `WP1-ONE-HUNDRED-TWENTIETH-SLICE.md` — moved
  the generic tool-executor implementation out of strangler services into its
  canonical packaged adapter while preserving the legacy import identity.
- WP1 One-Hundred-Twenty-Second Slice: `WP1-ONE-HUNDRED-TWENTY-SECOND-SLICE.md` —
  moved the stateful memory repository implementation out of generic strangler
  services into its canonical packaged adapter while preserving the legacy
  import identity.
- WP1 One-Hundred-Twenty-First Slice: `WP1-ONE-HUNDRED-TWENTY-FIRST-SLICE.md` —
  moved the pure prompt-section formatting helper into the domain boundary
  while preserving the server compatibility alias.
- WP1 One-Hundred-Twenty-Fourth Slice: `WP1-ONE-HUNDRED-TWENTY-FOURTH-SLICE.md` —
  moved the pure schema-gap formatting helper into the domain boundary while
  preserving the server compatibility alias.
- WP1 One-Hundred-Twenty-Fifth Slice: `WP1-ONE-HUNDRED-TWENTY-FIFTH-SLICE.md` —
  moved the legacy model gateway into its canonical packaged adapter.
- WP1 One-Hundred-Twenty-Sixth Slice: `WP1-ONE-HUNDRED-TWENTY-SIXTH-SLICE.md` —
  moved the pure cloud-model-name classifier into the domain boundary while
  preserving the server compatibility alias.
- WP1 One-Hundred-Twenty-Seventh Slice: `WP1-ONE-HUNDRED-TWENTY-SEVENTH-SLICE.md` —
  moved health-meter formatting into the domain boundary while preserving the
  server compatibility alias.
- WP1 One-Hundred-Twenty-Eighth Slice: `WP1-ONE-HUNDRED-TWENTY-EIGHTH-SLICE.md` —
  moved UnitOfWork ownership out of generic strangler services into its
  canonical packaged adapter while preserving the legacy import identity.
- WP1 One-Hundred-Twenty-Ninth Slice: `WP1-ONE-HUNDRED-TWENTY-NINTH-SLICE.md` —
  moved campaign-output matching into the domain boundary while preserving
  the server compatibility alias.
- WP1 One-Hundred-Thirtieth Slice: `WP1-ONE-HUNDRED-THIRTIETH-SLICE.md` —
  moved preference codec ownership into its canonical packaged adapter while
  preserving the legacy identity alias.
- WP1 One-Hundred-Thirty-First Slice: `WP1-ONE-HUNDRED-THIRTY-FIRST-SLICE.md` —
  moved model-usage count normalization into the domain boundary while
  preserving the server compatibility alias.
- WP1 One-Hundred-Thirty-Second Slice: `WP1-ONE-HUNDRED-THIRTY-SECOND-SLICE.md` —
  moved workflow loop-runner ownership into its canonical packaged adapter
  while preserving the legacy identity alias.
- WP1 One-Hundred-Thirty-Third Slice: `WP1-ONE-HUNDRED-THIRTY-THIRD-SLICE.md` —
  moved saved-workflow repository ownership into its canonical packaged adapter
  while preserving the legacy identity alias.
- WP1 One-Hundred-Thirty-Fourth Slice: `WP1-ONE-HUNDRED-THIRTY-FOURTH-SLICE.md` —
  moved campaign headline formatting into the domain boundary while
  preserving the server compatibility alias and durable pitfall visibility.
- WP1 One-Hundred-Thirty-Fifth Slice: `WP1-ONE-HUNDRED-THIRTY-FIFTH-SLICE.md` —
  moved campaign expected-output policy into the domain boundary while
  preserving the server compatibility alias and exact task verdicts.
- WP1 One-Hundred-Thirty-Sixth Slice: `WP1-ONE-HUNDRED-THIRTY-SIXTH-SLICE.md` —
  moved evaluation-history reader ownership to the canonical packaged adapter
  while retaining the legacy module and class identity compatibility surface.
- WP1 One-Hundred-Thirty-Seventh Slice: `WP1-ONE-HUNDRED-THIRTY-SEVENTH-SLICE.md` —
  moved read-only inspection composition to the canonical packaged adapter
  while retaining the legacy class identity compatibility surface.
- WP1 One-Hundred-Thirty-Eighth Slice: `WP1-ONE-HUNDRED-THIRTY-EIGHTH-SLICE.md` —
  moved campaign environment-failure classification into the pure domain
  boundary while preserving the server compatibility alias.
- WP1 One-Hundred-Thirty-Ninth Slice: `WP1-ONE-HUNDRED-THIRTY-NINTH-SLICE.md` —
  moved startup capability-policy ownership into the canonical packaged
  adapter while preserving the bootstrap compatibility surface.
- WP1 One-Hundred-Fortieth Slice: `WP1-ONE-HUNDRED-FORTIETH-SLICE.md` —
  moved learning-tier canonicalization into the pure domain boundary while
  preserving the server compatibility alias.
- WP1 One-Hundred-Forty-Second Slice: `WP1-ONE-HUNDRED-FORTY-SECOND-SLICE.md` —
  moved SPEC-5 CLI argument parsing into the canonical packaged input adapter
  while preserving the bootstrap compatibility import.
- WP1 One-Hundred-Forty-Third Slice: `WP1-ONE-HUNDRED-FORTY-THIRD-SLICE.md` —
  moved hosted-tier opt-in policy text into the pure domain boundary while
  preserving the server compatibility alias.
- WP1 One-Hundred-Forty-First Slice: `WP1-ONE-HUNDRED-FORTY-FIRST-SLICE.md` —
  moved the pure context token-estimate helper into the domain boundary while
  preserving the server compatibility alias.
- WP1 One-Hundred-Forty-Fourth Slice: `WP1-ONE-HUNDRED-FORTY-FOURTH-SLICE.md` —
  moved startup `RuntimeConfig` and environment normalization into a
  canonical packaged adapter while preserving bootstrap compatibility imports.
- WP1 One-Hundred-Forty-Fifth Slice: `WP1-ONE-HUNDRED-FORTY-FIFTH-SLICE.md` —
  moved model-usage provenance classification into the pure domain boundary
  while preserving the server compatibility alias.
- WP1 One-Hundred-Forty-Sixth Slice: `WP1-ONE-HUNDRED-FORTY-SIXTH-SLICE.md` —
  moved generic process-wide application lifecycle ownership into a canonical
  packaged adapter while preserving bootstrap lazy caching, atomic
  construction, reset behavior, and compatibility functions.
- WP1 One-Hundred-Forty-Seventh Slice: `WP1-ONE-HUNDRED-FORTY-SEVENTH-SLICE.md` —
  moved explicit SPEC-5 runtime graph assembly into a canonical packaged
  adapter while preserving the bootstrap compatibility surface and backend
  selection behavior.
- WP1 One-Hundred-Forty-Eighth Slice: `WP1-ONE-HUNDRED-FORTY-EIGHTH-SLICE.md` —
  moved the pure fanout generative-capability eligibility policy into the
  existing domain boundary while preserving the server compatibility alias.
- WP1 One-Hundred-Forty-Ninth Slice: `WP1-ONE-HUNDRED-FORTY-NINTH-SLICE.md` —
  moved bootstrap model-backend normalization and gateway construction into a
  canonical packaged factory while preserving the private bootstrap selector
  identity and existing transport behavior.
- WP1 One-Hundred-Fiftieth Slice: `WP1-ONE-HUNDRED-FIFTIETH-SLICE.md` —
  moved integer environment-option parsing into the platform boundary while
  preserving the server compatibility alias.
- WP1 One-Hundred-Fifty-First Slice: `WP1-ONE-HUNDRED-FIFTY-FIRST-SLICE.md` —
  moved response interaction-footer parsing into the pure domain boundary while
  preserving the server compatibility alias.
- WP1 One-Hundred-Fifty-Second Slice: `WP1-ONE-HUNDRED-FIFTY-SECOND-SLICE.md` —
  moved task persistence ownership into a canonical packaged adapter while
  preserving the `task_store` compatibility alias and leaving its event sink
  boundary unchanged.
- WP1 One-Hundred-Fifty-Third Slice: `WP1-ONE-HUNDRED-FIFTY-THIRD-SLICE.md` —
  moved the pure interactive control-command timeout policy into the domain
  boundary while preserving the server compatibility alias.
- WP1 One-Hundred-Fifty-Fourth Slice: `WP1-ONE-HUNDRED-FIFTY-FOURTH-SLICE.md` —
  moved checklist event-sink implementation ownership into its dedicated
  packaged task-event adapter while preserving the `task_store` compatibility
  alias.
- WP1 One-Hundred-Fifty-Sixth Slice: `WP1-ONE-HUNDRED-FIFTY-SIXTH-SLICE.md` —
  moved deterministic host-environment discovery into the canonical platform
  adapter while preserving the root compatibility import and shared cache.
- WP1 One-Hundred-Fifty-Fifth Slice: `WP1-ONE-HUNDRED-FIFTY-FIFTH-SLICE.md` —
  moved pure upstream HTTP `Retry-After` parsing into the domain boundary
  while preserving the root server compatibility alias.
- WP1 One-Hundred-Fifty-Eighth Slice: `WP1-ONE-HUNDRED-FIFTY-EIGHTH-SLICE.md` —
  moved fixed toolchain status-probe policy and canonical discovery lookup
  into the platform boundary while retaining bounded execution in the adapter.
- WP1 One-Hundred-Fifty-Seventh Slice: `WP1-ONE-HUNDRED-FIFTY-SEVENTH-SLICE.md` —
  moved pure model-error redaction and bounded-detail formatting into the
  domain boundary while preserving root server compatibility aliases.
- WP1 One-Hundred-Fifty-Ninth Slice: `WP1-ONE-HUNDRED-FIFTY-NINTH-SLICE.md` —
  moved model overflow-retry eligibility into the platform boundary while
  preserving root server compatibility aliases.
- WP1 One-Hundred-Sixtieth Slice: `WP1-ONE-HUNDRED-SIXTIETH-SLICE.md` —
  moved inference telemetry normalization into the canonical inference adapter
  package while preserving the legacy module's compatibility exports.
- WP1 One-Hundred-Sixty-Second Slice: `WP1-ONE-HUNDRED-SIXTY-SECOND-SLICE.md` —
  moved Ollama model-root path resolution into the canonical platform boundary
  while preserving the storage adapter compatibility export.
- WP1 One-Hundred-Sixty-Third Slice: `WP1-ONE-HUNDRED-SIXTY-THIRD-SLICE.md` —
  moved pure model request-timeout normalization into the canonical domain
  boundary while preserving the root server wrapper over the live ceiling.
- WP1 One-Hundred-Sixty-First Slice: `WP1-ONE-HUNDRED-SIXTY-FIRST-SLICE.md` —
  moved cancellation safety policy into the canonical domain boundary while
  preserving the root server compatibility alias.
- WP1 One-Hundred-Sixty-Fourth Slice: `WP1-ONE-HUNDRED-SIXTY-FOURTH-SLICE.md` —
  moved repository storage-error translation to a dedicated adapter boundary
  while preserving the task repository compatibility helper.
- WP1 One-Hundred-Sixty-Fifth Slice: `WP1-ONE-HUNDRED-SIXTY-FIFTH-SLICE.md` —
  moved the pure CPU-thread default policy into the platform environment-options
  boundary while preserving the root server compatibility alias.
- WP1 One-Hundred-Sixty-Sixth Slice: `WP1-ONE-HUNDRED-SIXTY-SIXTH-SLICE.md` —
  moved the read-only validated-configuration check factory into a packaged
  adapter while preserving the root doctor compatibility function.
- WP1 One-Hundred-Sixty-Seventh Slice: `WP1-ONE-HUNDRED-SIXTY-SEVENTH-SLICE.md` —
  moved pure interactive control-history message normalization into the domain
  boundary while preserving the root server compatibility alias.
- WP1 One-Hundred-Sixty-Eighth Slice: `WP1-ONE-HUNDRED-SIXTY-EIGHTH-SLICE.md` —
  moved pure secret-presence redaction into the platform boundary while keeping
  configuration ownership and redacted output compatibility intact.
- WP1 One-Hundred-Seventieth Slice: `WP1-ONE-HUNDRED-SEVENTIETH-SLICE.md` —
  moved pure model-tag parameter parsing into the packaged domain boundary
  while preserving the root hardware compatibility export.
- WP1 One-Hundred-Seventy-First Slice: `WP1-ONE-HUNDRED-SEVENTY-FIRST-SLICE.md` —
  moved the environment-backed approximate-location consent policy into the
  platform boundary while preserving the root server compatibility delegate.
- WP1 One-Hundred-Sixty-Ninth Slice: `WP1-ONE-HUNDRED-SIXTY-NINTH-SLICE.md` —
  moved the import-time running-source commit probe into the platform version
  boundary while preserving the root server compatibility delegate.
- WP1 One-Hundred-Seventy-Second Slice: `WP1-ONE-HUNDRED-SEVENTY-SECOND-SLICE.md` —
  moved pure model parameter-band, fit, and Q4 footprint policy into the
  packaged model-sizing domain boundary while preserving hardware exports.
- WP1 One-Hundred-Seventy-Fourth Slice: `WP1-ONE-HUNDRED-SEVENTY-FOURTH-SLICE.md` —
  moved environment-backed native/virtual context sizing into the packaged
  platform boundary while preserving the legacy root module identity.
- WP1 One-Hundred-Seventy-Third Slice: `WP1-ONE-HUNDRED-SEVENTY-THIRD-SLICE.md` —
  moved pure live cloud-model override selection into the packaged domain
  boundary while preserving the root server compatibility alias.
- WP1 One-Hundred-Seventy-Sixth Slice: `WP1-ONE-HUNDRED-SEVENTY-SIXTH-SLICE.md` —
  moved pure hardware-probe memory normalization into the packaged platform
  boundary while preserving the root hardware compatibility alias.
- WP1 One-Hundred-Seventy-Fifth Slice: `WP1-ONE-HUNDRED-SEVENTY-FIFTH-SLICE.md` —
  moved the pure context character-count token estimator into the packaged
  domain boundary while preserving the root server compatibility alias.
- WP1 One-Hundred-Seventy-Eighth Slice: `WP1-ONE-HUNDRED-SEVENTY-EIGHTH-SLICE.md` —
  moved doctor-check status coercion into the packaged domain boundary while
  preserving the root doctor compatibility alias.
- WP1 One-Hundred-Seventy-Ninth Slice: `WP1-ONE-HUNDRED-SEVENTY-NINTH-SLICE.md` —
  moved local Ollama runtime-option policy into the packaged platform boundary
  while preserving the root server compatibility delegate.
- WP1 One-Hundred-Eightieth Slice: `WP1-ONE-HUNDRED-EIGHTIETH-SLICE.md` —
  moved doctor-check result normalization into the packaged domain boundary
  while preserving the root doctor compatibility alias.
- WP1 One-Hundred-Eighty-Second Slice: `WP1-ONE-HUNDRED-EIGHTY-SECOND-SLICE.md` —
  moved display-adapter vendor normalization into the packaged platform
  boundary while preserving the root hardware compatibility alias.
- WP1 One-Hundred-Eighty-First Slice: `WP1-ONE-HUNDRED-EIGHTY-FIRST-SLICE.md` —
  moved master-orchestration timeout normalization into the packaged domain
  boundary while preserving the root server compatibility delegate.
- WP1 One-Hundred-Eighty-Fourth Slice: `WP1-ONE-HUNDRED-EIGHTY-FOURTH-SLICE.md` —
  moved pure artifact execution-risk denial policy into the packaged domain
  boundary while preserving the root compatibility export.
- WP1 One-Hundred-Eighty-Third Slice: `WP1-ONE-HUNDRED-EIGHTY-THIRD-SLICE.md` —
  moved pure chat code-gate target selection into the packaged domain boundary
  while preserving the root server compatibility delegate.
- WP1 One-Hundred-Eighty-Sixth Slice: `WP1-ONE-HUNDRED-EIGHTY-SIXTH-SLICE.md` —
  moved pure integrated-versus-discrete accelerator classification into the
  packaged platform boundary while preserving the root hardware delegate.
- WP1 One-Hundred-Eighty-Fifth Slice: `WP1-ONE-HUNDRED-EIGHTY-FIFTH-SLICE.md` —
  moved pure valid-tier-name presentation into the packaged domain boundary
  while preserving the root server compatibility delegate.
- WP1 One-Hundred-Eighty-Eighth Slice: `WP1-ONE-HUNDRED-EIGHTY-EIGHTH-SLICE.md` —
  moved skipped doctor-result construction into the packaged domain boundary
  while preserving the root doctor compatibility delegate.
- WP1 One-Hundred-Ninetieth Slice: `WP1-ONE-HUNDRED-NINETIETH-SLICE.md` —
  moved bounded toolchain-probe output normalization and redaction into the
  packaged platform policy while preserving the root compatibility delegate.
- WP1 One-Hundred-Eighty-Ninth Slice: `WP1-ONE-HUNDRED-EIGHTY-NINTH-SLICE.md` —
  moved Ollama inventory-envelope validation into the packaged adapter
  boundary while preserving the root server compatibility delegate.
- WP1 One-Hundred-Ninety-Second Slice: `WP1-ONE-HUNDRED-NINETY-SECOND-SLICE.md` —
  moved doctor check-registry normalization into the packaged domain boundary
  while preserving the root doctor compatibility delegate.
- WP1 One-Hundred-Ninety-First Slice: `WP1-ONE-HUNDRED-NINETY-FIRST-SLICE.md` —
  moved local-runtime summary projection into the packaged platform boundary
  while preserving the root server compatibility delegate.
- WP1 One-Hundred-Ninety-Third Slice: `WP1-ONE-HUNDRED-NINETY-THIRD-SLICE.md` —
  moved root server context-size selection into the packaged platform adapter
  while preserving the server compatibility delegates.
- WP1 One-Hundred-Ninety-Fourth Slice: `WP1-ONE-HUNDRED-NINETY-FOURTH-SLICE.md` —
  moved host-platform probing into the packaged hardware platform boundary
  while preserving the root hardware compatibility delegate.
- WP1 One-Hundred-Ninety-Sixth Slice: `WP1-ONE-HUNDRED-NINETY-SIXTH-SLICE.md` —
  moved CPU-count probing into the packaged hardware platform boundary while
  preserving the root hardware compatibility alias.
- WP1 One-Hundred-Ninety-Fifth Slice: `WP1-ONE-HUNDRED-NINETY-FIFTH-SLICE.md` —
  moved the bounded lesson-distillation timeout policy into the packaged
  domain boundary while preserving the root server compatibility wrapper.
- WP1 One-Hundred-Ninety-Eighth Slice: `WP1-ONE-HUNDRED-NINETY-EIGHTH-SLICE.md` —
  moved pure NPU identification policy into the packaged platform boundary
  while preserving system-profile compatibility aliases.
- WP1 One-Hundred-Ninety-Seventh Slice: `WP1-ONE-HUNDRED-NINETY-SEVENTH-SLICE.md` —
  moved pure permission-mode context rendering into the packaged domain
  boundary while preserving the root server compatibility helper.
- WP1 One-Hundred-Ninety-Ninth Slice: `WP1-ONE-HUNDRED-NINETY-NINTH-SLICE.md` —
  moved reasoning-exposure environment policy into the packaged platform
  boundary while preserving the root server compatibility helper.
- WP1 Two-Hundredth Slice: `WP1-TWO-HUNDREDTH-SLICE.md` —
  moved scalar compatibility-environment coercion into the packaged platform
  configuration-environment boundary while preserving config helper identity.
- WP1 Two-Hundred-First Slice: `WP1-TWO-HUNDRED-FIRST-SLICE.md` —
  moved bounded local model retry configuration and exponential backoff into
  the packaged platform boundary while preserving root server wrappers.
- WP1 Two-Hundred-Second Slice: `WP1-TWO-HUNDRED-SECOND-SLICE.md` —
  moved total physical RAM probing into the packaged hardware platform boundary
  while preserving the legacy hardware probe alias.
- WP1 Two-Hundred-Third Slice: `WP1-TWO-HUNDRED-THIRD-SLICE.md` —
  moved the private-COT environment opt-in policy into the packaged platform
  boundary while preserving the root server compatibility helper.
- WP1 Two-Hundred-Fourth Slice: `WP1-TWO-HUNDRED-FOURTH-SLICE.md` —
  moved bounded toolchain process-tree teardown into the packaged adapter
  boundary while preserving the legacy toolchain-status delegate.
- WP1 Two-Hundred-Sixth Slice: `WP1-TWO-HUNDRED-SIXTH-SLICE.md` —
  moved the standalone client's local-fallback environment policy into the
  packaged platform boundary while preserving the legacy client export.
- WP1 Two-Hundred-Fifth Slice: `WP1-TWO-HUNDRED-FIFTH-SLICE.md` —
  moved in-band model-error extraction into the packaged domain formatting
  boundary while preserving the root server compatibility wrapper.
- WP1 Two-Hundred-Seventh Slice: `WP1-TWO-HUNDRED-SEVENTH-SLICE.md` —
  moved model transport error-detail extraction and formatting into the
  packaged adapter boundary while preserving root server compatibility
  delegates.
- WP1 Two-Hundred-Eighth Slice: `WP1-TWO-HUNDRED-EIGHTH-SLICE.md` —
  moved system-profile floating-point environment override parsing into the
  packaged configuration-environment boundary while preserving the existing
  system-profile alias.
- WP1 Two-Hundred-Tenth Slice: `WP1-TWO-HUNDRED-TENTH-SLICE.md` —
  moved best-effort optional configuration loading into the packaged bootstrap
  boundary while preserving the doctor compatibility delegate.
- WP1 Two-Hundred-Ninth Slice: `WP1-TWO-HUNDRED-NINTH-SLICE.md` —
  moved hosted-model prediction-budget normalization into the packaged domain
  boundary while preserving the root server compatibility helper.
- WP1 Two-Hundred-Eleventh Slice: `WP1-TWO-HUNDRED-ELEVENTH-SLICE.md` —
  moved context-overflow retry payload compaction into the packaged context
  domain boundary while preserving the root server compatibility helper.
- WP1 Two-Hundred-Twelfth Slice: `WP1-TWO-HUNDRED-TWELFTH-SLICE.md` —
  moved the concrete NVIDIA GPU memory probe into the packaged platform
  hardware-probe boundary while preserving the legacy hardware alias.
- WP1 Two-Hundred-Fourteenth Slice: `WP1-TWO-HUNDRED-FOURTEENTH-SLICE.md` —
  moved standalone-client chat request construction into the packaged adapter
  boundary while preserving the root client compatibility delegate.
- WP1 Two-Hundred-Thirteenth Slice: `WP1-TWO-HUNDRED-THIRTEENTH-SLICE.md` —
  moved leading JSON-object parsing for schema-constrained responses into the
  packaged domain schema-policy boundary while preserving the root protocol
  error contract.
- WP1 Two-Hundred-Fifteenth Slice: `WP1-TWO-HUNDRED-FIFTEENTH-SLICE.md` —
  moved live cloud-tier legacy-repair policy into the packaged domain boundary
  while preserving the root server compatibility wrapper.
- WP1 Two-Hundred-Sixteenth Slice: `WP1-TWO-HUNDRED-SIXTEENTH-SLICE.md` —
  moved child-process environment secret/control-name classification into the
  packaged platform policy boundary while preserving the logging compatibility
  alias.
- WP1 Two-Hundred-Eighteenth Slice: `WP1-TWO-HUNDRED-EIGHTEENTH-SLICE.md` —
  moved normalized accelerator-record construction into the packaged platform
  hardware-identity boundary while preserving the legacy hardware alias.
- WP1 Two-Hundred-Twentieth Slice: `WP1-TWO-HUNDRED-TWENTIETH-SLICE.md` —
  moved host-probe filesystem text reading into the packaged platform
  hardware-probe boundary while preserving the legacy hardware alias.
- WP1 Two-Hundred-Twenty-First Slice: `WP1-TWO-HUNDRED-TWENTY-FIRST-SLICE.md` —
  moved Ollama endpoint display ownership into the packaged endpoint policy
  while preserving the root zero-argument compatibility alias.
- WP1 Two-Hundred-Twenty-Fourth Slice: `WP1-TWO-HUNDRED-TWENTY-FOURTH-SLICE.md` —
  moved standalone-client argv and environment configuration resolution into
  the packaged client adapter while preserving root compatibility delegates.
- WP1 Two-Hundred-Twenty-Fifth Slice: `WP1-TWO-HUNDRED-TWENTY-FIFTH-SLICE.md` —
  moved live-process fingerprint selection into the canonical process-liveness
  adapter while preserving the `ProcessProbeAdapter.identity` port surface.
- WP1 Two-Hundred-Thirty-Fourth Slice: `WP1-TWO-HUNDRED-THIRTY-FOURTH-SLICE.md` —
  moved cooperative cancellation ownership into the packaged process boundary
  while preserving packaged-shutdown and root compatibility aliases.
- WP1 Two-Hundred-Seventeenth Slice: `WP1-TWO-HUNDRED-SEVENTEENTH-SLICE.md` —
  moved bounded integer-limit normalization into the packaged domain boundary
  while preserving the root server compatibility delegate.
- WP1 Two-Hundred-Nineteenth Slice: `WP1-TWO-HUNDRED-NINETEENTH-SLICE.md` —
  moved the clean-generation no-retrieval policy into the packaged domain
  boundary while preserving the root server compatibility wrapper.
- WP1 Two-Hundred-Twenty-Third Slice: `WP1-TWO-HUNDRED-TWENTY-THIRD-SLICE.md` —
  moved self-heal doctor-result classification into the packaged bootstrap
  doctor boundary while preserving root compatibility wiring.
- WP1 Two-Hundred-Twenty-Sixth Slice: `WP1-TWO-HUNDRED-TWENTY-SIXTH-SLICE.md` —
  moved system-profile boolean environment-override parsing into the packaged
  configuration-environment boundary while preserving the `_env_bool` alias.
- WP1 Two-Hundred-Thirtieth Slice: `WP1-TWO-HUNDRED-THIRTIETH-SLICE.md` —
  moved the pure local-observability percentile helper into the packaged
  observability boundary while preserving the `_percentile` compatibility alias.
- WP1 Two-Hundred-Thirty-Third Slice: `WP1-TWO-HUNDRED-THIRTY-THIRD-SLICE.md` —
  consolidated the unstamped build-info commit probe onto the packaged version
  boundary while preserving root and private compatibility aliases.
- WP1 Two-Hundred-Thirty-Sixth Slice: `WP1-TWO-HUNDRED-THIRTY-SIXTH-SLICE.md` —
  moved the debug-dump redaction import to the packaged logging boundary while
  preserving the `debug_dump.Redactor` compatibility alias.
- WP1 Two-Hundred-Thirty-Eighth Slice: `WP1-TWO-HUNDRED-THIRTY-EIGHTH-SLICE.md` —
  moved context-health text formatting to the packaged observability boundary
  while preserving the generic packaged formatter alias.
- WP1 Two-Hundred-Twenty-Eighth Slice: `WP1-TWO-HUNDRED-TWENTY-EIGHTH-SLICE.md` —
  moved launcher-health token status ownership into the packaged health
  status boundary while preserving the root `sonder_health` alias.
- WP1 Two-Hundred-Twenty-Second Slice: `WP1-TWO-HUNDRED-TWENTY-SECOND-SLICE.md` —
  moved accelerator-inventory de-duplication into the packaged hardware
  adapter boundary while preserving the legacy hardware compatibility alias.
- WP1 Two-Hundred-Forty-Second Slice: `WP1-TWO-HUNDRED-FORTY-SECOND-SLICE.md` —
  moved the remaining pure hardware sizing helpers into the packaged domain
  boundary and documented the packaged accelerator/platform probe seams.
- WP1 Two-Hundred-Forty-Sixth Slice: `WP1-TWO-HUNDRED-FORTY-SIXTH-SLICE.md` —
  moved the remaining root doctor configuration-check policy into the packaged
  bootstrap/config boundary while preserving the root compatibility delegate.
- WP1 Two-Hundred-Forty-Third Slice: `WP1-TWO-HUNDRED-FORTY-THIRD-SLICE.md` — moved speculative-execution configuration helpers to the packaged platform boundary while preserving root helper aliases and the packaged domain safety policy.
- WP1 Two-Hundred-Forty-Seventh Slice: `WP1-TWO-HUNDRED-FORTY-SEVENTH-SLICE.md` — moved pure agent tool-invocation mutation policy to the packaged domain boundary while preserving root mutation tool-set and predicate aliases.
- WP1 Two-Hundred-Ninety-Sixth Slice: `WP1-TWO-HUNDRED-NINETY-SIXTH-SLICE.md` — pure fanout prompt-echo redaction now lives in `sonder_runtime.domain.fanout_redaction`, preserving the root `_fanout_redact_prompt_echo` alias
- WP1 Two-Hundred-Ninety-Seventh Slice: `WP1-TWO-HUNDRED-NINETY-SEVENTH-SLICE.md` — pure agent decision parsing now lives in `sonder_runtime.domain.agents.decision_parsing`, preserving the root `_extract_agent_json` alias
- WP1 Two-Hundred-Ninety-Eighth Slice: `WP1-TWO-HUNDRED-NINETY-EIGHTH-SLICE.md` — pure improvement report rendering now lives in `sonder_runtime.domain.improvement_report_formatting`, preserving the root `format_improvement_report` alias
- WP1 Two-Hundred-Ninety-Ninth Slice: `WP1-TWO-HUNDRED-NINETY-NINTH-SLICE.md` — the pure natural-language model and fanout request grammar now lives in `sonder_runtime.domain.natural_model_request`, preserving the root `natural_model_request` and `_fanout_profile_scope` delegates and the selector constant aliases
- WP1 Three-Hundredth Slice: `WP1-THREE-HUNDREDTH-SLICE.md` — the pure agent observation prompt framing (untrusted-data envelope, clipping and compaction) now lives in `sonder_runtime.domain.agents.observation_prompt`, preserving the root `_agent_observation_prompt` family aliases
- WP1 Three-Hundred-First Slice: `WP1-THREE-HUNDRED-FIRST-SLICE.md` — pure runtime source update rendering and its presentation-only eligibility verdict now live in `sonder_runtime.domain.updates.runtime_update_formatting`, preserving the root `_runtime_update_format` and `_runtime_update_eligibility` delegates
- WP1 Three-Hundred-Second Slice: `WP1-THREE-HUNDRED-SECOND-SLICE.md` — pure MCP runtime status rendering and the content-free refresh-error reducer now live in `sonder_runtime.domain.mcp_runtime_formatting`, preserving the root `format_mcp_runtime` delegate and the `_safe_mcp_error` alias
- WP1 Three-Hundred-Third Slice: `WP1-THREE-HUNDRED-THIRD-SLICE.md` — pure fanout receipt limits and the immutable admission record now live in `sonder_runtime.domain.fanout_admission`, preserving the root `_fanout_limits` alias and the `_fanout_admission` delegate
- WP1 Three-Hundred-Fourth Slice: `WP1-THREE-HUNDRED-FOURTH-SLICE.md` — pure agent tool-name canonicalization now lives in `sonder_runtime.domain.agents.tool_naming`, preserving the root `_AGENT_TOOL_ALIASES` and `_canonical_agent_tool_name` aliases
- WP1 Three-Hundred-Fifth Slice: `WP1-THREE-HUNDRED-FIFTH-SLICE.md` — the pure agent claim-review policy (negative-claim grammar, exact anchors, reviewer vocabulary and the exact-search action) now lives in `sonder_runtime.domain.agents.claim_review`, preserving the root constant and anchor aliases and the three hosted-policy delegates
- WP1 Three-Hundred-Sixth Slice: `WP1-THREE-HUNDRED-SIXTH-SLICE.md` — pure agent evidence-quality checks and verifier-reach classification now live in `sonder_runtime.domain.agents.evidence_quality` and `sonder_runtime.domain.agents.verification_reach`, preserving the root `_ensemble_codegen_build_succeeded` and `_AGENT_VERIFICATION_TOOLS` aliases and the `_agent_tool_observation_ok` and `_agent_verifier_reachable` delegates
- WP1 Three-Hundred-Seventh Slice: `WP1-THREE-HUNDRED-SEVENTH-SLICE.md` — pure agent activity-command rendering (argv and batch-operation normalization plus the per-tool command line) now lives in `sonder_runtime.domain.agents.activity_command`, preserving the root `_agent_activity_command`, `_activity_argv`, `_agent_argv` and `_batch_agent_operations` aliases
- WP1 Three-Hundred-Eighth Slice: `WP1-THREE-HUNDRED-EIGHTH-SLICE.md` — pure ensemble synthesis prompts (candidate serialization, the untrusted-reference envelope, and the prose and code synthesis contracts) now live in `sonder_runtime.domain.ensemble_synthesis`, preserving the root `_ensemble_*` aliases
- WP1 Three-Hundred-Ninth Slice: `WP1-THREE-HUNDRED-NINTH-SLICE.md` — pure context-pack argument normalization (paths, bounded integers and the UTF-8 byte prefix) now lives in `sonder_runtime.domain.context.pack_arguments`, preserving the root `_context_pack_*` aliases
- WP1 Three-Hundred-Tenth Slice: `WP1-THREE-HUNDRED-TENTH-SLICE.md` — pure local tier model-binding checks (installed-tag matching and the catalog capability mismatch) now live in `sonder_runtime.domain.runtime_model_binding`, preserving the root `_runtime_model_is_installed` and `_runtime_model_capability_error` aliases
- WP1 Three-Hundred-Eleventh Slice: `WP1-THREE-HUNDRED-ELEVENTH-SLICE.md` — pure runtime recovery-stash rendering now lives in `sonder_runtime.domain.updates.stash_formatting`, preserving the root `_runtime_stash_format` alias
- WP1 Three-Hundred-Twelfth Slice: `WP1-THREE-HUNDRED-TWELFTH-SLICE.md` — pure hosted and local thinking controls (per-model hosted policy, the think=false allow-list, the think-option refusal recognizer and the local thinking budget) now live in `sonder_runtime.domain.thinking_controls`, preserving the root aliases and the `_apply_cloud_thinking_policy` delegate
- WP1 Three-Hundred-Thirteenth Slice: `WP1-THREE-HUNDRED-THIRTEENTH-SLICE.md` — pure fanout receipt safety (the credential scrubber over redacted answers and the immutable target-snapshot check) now lives in `sonder_runtime.domain.fanout_receipts`, preserving the root `_fanout_safe_answer` alias and the `_fanout_snapshot_allows` delegate
- WP1 Three-Hundred-Fourteenth Slice: `WP1-THREE-HUNDRED-FOURTEENTH-SLICE.md` — the pure empty-model-response description now lives in `sonder_runtime.domain.model_response_detail`, preserving the root `_empty_model_response_detail` alias
- WP1 Three-Hundred-Fifteenth Slice: `WP1-THREE-HUNDRED-FIFTEENTH-SLICE.md` — pure loop action resolution (the non-tool action table, the tool that actually runs, and the success-prefix verdict) now lives in `sonder_runtime.domain.loop_actions`, preserving the root `_LOOP_ACTION_TOOLS` and `_loop_action_tool` aliases and the `_loop_verdict_result` delegate
- WP1 Three-Hundred-Sixteenth Slice: `WP1-THREE-HUNDRED-SIXTEENTH-SLICE.md` — pure serve-target selection policy (explicit selection and the cloud availability-fallback rule) now lives in `sonder_runtime.domain.serve_selection`, preserving the root `_allow_cloud_fallback_for_target` and `_explicit_serve_selection` aliases
- WP1 Three-Hundred-Seventeenth Slice: `WP1-THREE-HUNDRED-SEVENTEENTH-SLICE.md` — pure context compaction plan rendering now lives in `sonder_runtime.domain.context.compaction_plan_formatting`, preserving the root `format_context_compaction_plan` alias
- WP1 Three-Hundred-Eighteenth Slice: `WP1-THREE-HUNDRED-EIGHTEENTH-SLICE.md` — fanout transport-failure classification and safe receipt rendering now live in `sonder_runtime.adapters.fanout_failures`, preserving the root `_fanout_failure_class`, `_fanout_safe_error` and `_fanout_no_eligible_models_error` aliases
- WP1 Three-Hundred-Nineteenth Slice: `WP1-THREE-HUNDRED-NINETEENTH-SLICE.md` — the model-call contracts for empty-response metadata and the offload schema argument now live in `sonder_runtime.adapters.model_response_metadata` and `sonder_runtime.adapters.offload_schema_argument`, preserving the root `_response_error_metadata` and `_parse_schema_arg` aliases
- WP1 Three-Hundred-Twentieth Slice: `WP1-THREE-HUNDRED-TWENTIETH-SLICE.md` — stable agent call signatures for de-duplicating equivalent tool calls now live in `sonder_runtime.adapters.agent_call_signature`, preserving the root `_agent_call_signature` delegate
- WP1 Three-Hundred-Twenty-First Slice: `WP1-THREE-HUNDRED-TWENTY-FIRST-SLICE.md` — the pure campaign task prompt now lives in `sonder_runtime.domain.campaign_prompt`, preserving the root `_campaign_prompt` delegate
- WP1 Three-Hundred-Twenty-Second Slice: `WP1-THREE-HUNDRED-TWENTY-SECOND-SLICE.md` — pure autopilot command program extraction now lives in `sonder_runtime.domain.automation.command_programs`, preserving the root `_autopilot_command_programs` alias
- WP1 Three-Hundred-Twenty-Third Slice: `WP1-THREE-HUNDRED-TWENTY-THIRD-SLICE.md` — agent decision generation with bounded format repair now lives in `sonder_runtime.adapters.agent_decision_generation`, preserving the root `_AGENT_DECISION_REPAIR_LIMIT` alias and the `_agent_generate_decision` delegate
- WP1 Three-Hundred-Twenty-Fourth Slice: `WP1-THREE-HUNDRED-TWENTY-FOURTH-SLICE.md` — hard-bounded hosted agent generation and its per-call and total ceilings now live in `sonder_runtime.adapters.bounded_cloud_generation`, preserving the root `_bounded_cloud_agent_generate`, `_CLOUD_AGENT_NUM_PREDICT` and `_CLOUD_AGENT_OUTPUT_BUDGET` aliases
- WP1 Three-Hundred-Twenty-Fifth Slice: `WP1-THREE-HUNDRED-TWENTY-FIFTH-SLICE.md` — advisory fanout model-health recording and cooldowns now live in `sonder_runtime.adapters.fanout_health`, preserving the root `_fanout_health` delegate
- WP1 Three-Hundred-Twenty-Sixth Slice: `WP1-THREE-HUNDRED-TWENTY-SIXTH-SLICE.md` — serializable fanout receipts now live in `sonder_runtime.adapters.fanout_receipt`, preserving the root `_fanout_receipt` delegate
- WP1 Three-Hundred-Twenty-Seventh Slice: `WP1-THREE-HUNDRED-TWENTY-SEVENTH-SLICE.md` — the agent work-coverage family (mutation records, path containment, the no-op flag and build-driver tables, and the validation and verification coverage predicates) now lives in `sonder_runtime.adapters.agent_work_coverage`, preserving every root alias
- WP1 Three-Hundred-Twenty-Eighth Slice: `WP1-THREE-HUNDRED-TWENTY-EIGHTH-SLICE.md` — the bounded repo-repair pytest runner now lives in `sonder_runtime.adapters.repo_repair_runner`, preserving the root `_repo_repair_pytest` alias
- WP1 Three-Hundred-Twenty-Ninth Slice: `WP1-THREE-HUNDRED-TWENTY-NINTH-SLICE.md` — pure Ollama catalog parsing (names, records, installed snapshot, tag revision and exact resolution) now lives in `sonder_runtime.domain.model_catalog`; the root discovery functions keep the fetch and every monkeypatch seam as thin delegates
- WP1 Three-Hundred-Thirtieth Slice: `WP1-THREE-HUNDRED-THIRTIETH-SLICE.md` — the hosted K3-to-K2.7 availability fallback now lives in `sonder_runtime.adapters.cloud_fallback`, preserving the root `_chat_request_with_cloud_fallback` and `_cloud_extra_usage_fallback` delegates
- WP1 Three-Hundred-Thirty-First Slice: `WP1-THREE-HUNDRED-THIRTY-FIRST-SLICE.md` — the pure fanout no-load residency fence now lives in `sonder_runtime.domain.fanout_residency`, preserving the root `_fanout_dispatch_residency_reason` delegate
- WP1 Three-Hundred-Thirty-Second Slice: `WP1-THREE-HUNDRED-THIRTY-SECOND-SLICE.md` — database-backed session turn claims now live in `sonder_runtime.adapters.session_turn_claims`, preserving the root `_acquire_persistent_session_turn` and `_release_persistent_session_turn` delegates
- WP1 Three-Hundred-Thirty-Third Slice: `WP1-THREE-HUNDRED-THIRTY-THIRD-SLICE.md` — compare-and-swap persistence of a verified code repair now lives in `sonder_runtime.adapters.code_repair_persistence`, preserving the root `_persist_verified_code_repair` delegate
- WP1 Three-Hundred-Thirty-Fourth Slice: `WP1-THREE-HUNDRED-THIRTY-FOURTH-SLICE.md` — project-scoped path key lookup now lives in sonder_runtime.domain.project_scope_keys, preserving the root _project_scoped_path_key compatibility alias
