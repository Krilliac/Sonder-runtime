# WP1 REPL status/model-selection facade

This slice extracts the bounded presentation policy for the REPL's tier,
status, and context-health route family. The new files under
`sonder_runtime/interfaces/repl/facades/` are root-free: they perform no
provider I/O, import no `server` module, and use no dynamic import bypass.

## Migrated behavior

- Provider catalog payload parsing, including the distinction between an
  unavailable catalog and a reachable empty catalog.
- Deterministic tier fallback/availability policy, withheld-tier reasons, and
  model/tier completion choices.
- Known/unknown execution lane status normalization and prompt text.
- Bounded context-health normalization (`used`, `limit`, `left`) with
  fail-closed handling for malformed or unavailable snapshots.

`repl.py` retains the command wiring and delegates these policies through
`ModelSelectionFacade`, `ExecutionStatusFacade`, and `ContextHealthFacade`.
Unrelated legacy commands were not rewritten.

## Exact remaining legacy imports for this route family

These imports remain in `sonder_runtime/interfaces/repl/repl.py` deliberately:

- `import server` — composition-root adapter for `/api/tags` discovery,
  `TIERS`/availability and cloud policy, discovered-model capability checks,
  and the existing execution/context snapshot providers. No application port
  currently exposes all of those legacy facts as one REPL catalog port.
- `import sonder_runtime.adapters.observability.activity_tracker as
  activity_tracker` — existing latest-turn telemetry source used by the
  composer; it is not part of tier selection or context-health normalization.
- `from sonder_runtime.adapters.observability.repl_formatting import
  elapsed_label as _elapsed_label` — existing presentation-only elapsed-time
  formatter used by the composer.

The other top-level imports in `repl.py` belong to unrelated legacy command
families and remain intentionally untouched by this bounded migration. The
facade package itself imports only the Python standard library.

## Evidence

`tests/test_wp1_repl_facade.py` verifies root-free AST boundaries, catalog
normalization, deterministic selection policy, and fail-closed status/context
display. Formal specification checkboxes are intentionally unchanged.
