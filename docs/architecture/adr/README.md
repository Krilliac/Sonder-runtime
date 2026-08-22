# Historical architecture ADR directory

This directory is retained for the numbered architecture-program ADR series.
Those records are historical decisions and remain useful for tracing why the
early modular-monolith boundaries were chosen. They are not the namespace for
new decisions.

## Namespace policy

- New ADRs belong under `docs/adr/`.
- New filenames must be globally unique and use
  `ADR-YYYY-MM-DD-<slug>.md`, for example
  `ADR-2026-08-20-event-sourced-sessions.md`.
- The existing `ADR-001` through `ADR-009` files here are retained without
  renumbering so historical links remain valid.
- The numeric `ADR-001` through `ADR-006` files under `docs/adr/` are likewise
  historical SPEC-5-era records. Their reused numbers do not create a new ADR
  identifier.
- A newer date-prefixed ADR or the master specification may explicitly
  supersede an older decision; until then, cite the older path and state its
  historical scope.

## Historical records

| ADR | Record |
|---|---|
| ADR-001 | [`ADR-001-modular-monolith.md`](ADR-001-modular-monolith.md) |
| ADR-002 | [`ADR-002-ollama-external.md`](ADR-002-ollama-external.md) |
| ADR-003 | [`ADR-003-sqlite-per-domain.md`](ADR-003-sqlite-per-domain.md) |
| ADR-004 | [`ADR-004-ports-and-adapters.md`](ADR-004-ports-and-adapters.md) |
| ADR-005 | [`ADR-005-operation-context.md`](ADR-005-operation-context.md) |
| ADR-006 | [`ADR-006-no-orm.md`](ADR-006-no-orm.md) |
| ADR-007 | [`ADR-007-compat-shims.md`](ADR-007-compat-shims.md) |
| ADR-008 | [`ADR-008-local-events.md`](ADR-008-local-events.md) |
| ADR-009 | [`ADR-009-local-observability.md`](ADR-009-local-observability.md) |
