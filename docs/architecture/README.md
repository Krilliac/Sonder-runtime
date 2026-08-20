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

Two historical ADR series exist and contain overlapping numbers:

- `docs/adr/` records SPEC-5-era product decisions.
- `docs/architecture/adr/` records the preceding architecture-program decisions.

New decisions must use a globally unique date-prefixed filename under `docs/adr/`, for
example `ADR-2026-08-19-event-sourced-sessions.md`. Existing ADRs retain their paths so
old links remain valid. Their decisions remain authoritative unless the master spec or a
newer ADR explicitly supersedes them.

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
