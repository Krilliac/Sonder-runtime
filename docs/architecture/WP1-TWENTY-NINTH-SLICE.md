# WP1 Twenty-Ninth Slice: Package the Migration Registry

Status: implemented on `agent/wp1-execution-status`.

## Scope

The migration registry now lives at
`sonder_runtime.adapters.persistence.migrations`. Its store-path registry is
independent of individual store modules, eliminating a persistence-layer import
cycle while preserving the immutable repository-level `migrations/` baseline
tree and package manifest.

The queued-action store consumes the packaged registry, and the retired root
`sonder_migrations.py` implementation is no longer part of the runtime import
surface. The root compatibility aliases for the immutable autopilot, fleet,
and queued-action migration boundaries remain explicit and bounded.

## Evidence

- Queue, migration, architecture, and terminology regressions: **77 passed**.
- `python -m compileall -q sonder_runtime tests`: passes.
- `scripts/check_architecture.py`: passes with three remaining legacy roots.
- `scripts/check_requirement_evidence.py`: passes.
- `git diff --cached --check`: passes.

## Remaining boundary

The remaining implementation roots are `server`, `autopilot_store`, and
`fleet_store`; the latter two are compatibility aliases retained for their
immutable migration imports. The server composition root is the next major
boundary.
