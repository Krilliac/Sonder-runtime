# WP1 Fiftieth Slice: fleet-store legacy boundary ratchet

Status: implemented on `agent/wp1-execution-status`.

## Scope

The production caller audit found no live import of the root `fleet_store`
module. `master_orchestrator.py` and the production tests use
`sonder_runtime.adapters.persistence.fleet_store`; the only root import is
the immutable `migrations/fleet/0001_baseline.py` replay path. The root
`fleet_store.py` alias therefore remains packaged for migration compatibility,
but is no longer an active package legacy-root allowance.

This slice changes only the architecture policy and its focused regression
expectations. It does not rewrite the immutable migration or remove the
compatibility alias.

## Evidence

- `rg` source audit: no `fleet_store` root import under `sonder_runtime/` or
  other live production callers; the immutable fleet migration is the sole
  root import.
- `python -m pytest -q tests/production/test_architecture.py`: passes.
- `python scripts/check_architecture.py`: passes with only `server` as an
  active legacy-root allowance.
- `python scripts/check_requirement_evidence.py`: passes.
- `python -m compileall -q sonder_runtime server.py`: passes.
- `git diff --cached --check` and `git diff --check`: pass.

The compatibility alias and migration exception remain explicitly checked by
the packaging and architecture gates.
