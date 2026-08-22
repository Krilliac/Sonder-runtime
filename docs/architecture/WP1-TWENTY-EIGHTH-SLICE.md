# WP1 Twenty-Eighth Slice: Package the REPL Interface

Status: implemented on `agent/wp1-execution-status`.

## Scope

The interactive REPL now lives at
`sonder_runtime.interfaces.repl.repl`. The package CLI, REPL catalog/input,
live-reload, capability-query, permission-policy, training, and related tests
use the package-qualified interface. Root `sonder_repl.py` is retired.

The interface retains a narrow exact-file policy for its current server/tool
composition dependencies; the default interface dependency rules remain
unchanged.

## Evidence

- REPL catalog/input, capability queries, permission display, live reload, git
  tools, training-cap, and production architecture regression: **225 passed,
  1 skipped** in the focused run.
- `scripts/check_architecture.py`: passes with four remaining legacy roots.
- `scripts/check_requirement_evidence.py`: passes.
- `git diff --cached --check`: passes.

## Remaining boundary

Remaining roots are `server`, `sonder_migrations`, and the immutable
autopilot/fleet migration aliases. The server is the next major composition
root; migration aliases remain until their historical byte-identity boundary
is replaced.
