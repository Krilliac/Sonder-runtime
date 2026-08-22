# WP1 Nineteenth Slice: Package Fleet Persistence

Status: implemented on `agent/wp1-execution-status`.

## Scope

The canonical fleet store now lives at
`sonder_runtime.adapters.persistence.fleet_store`. The orchestrator,
migration registry, test fixtures, fleet provenance, and automation tests use
the package-qualified implementation. The root `fleet_store.py` is a narrow
delegation alias retained only for the immutable
`migrations/fleet/0001_baseline.py` migration, with the exception explicitly
checked by the architecture gate.

## Evidence

- Fleet store, provenance, automation, and production architecture regression:
  **127 passed, 1 skipped**.
- `scripts/check_architecture.py`: passes.
- `scripts/check_requirement_evidence.py`: passes for the staged tree.
- `git diff --cached --check`: passes.

## Remaining boundary

The root alias remains until immutable migration replay can use a package-native
archive without changing deployed migration bytes. Remaining root services and
stores continue as separate behavior-preserving migration slices.
