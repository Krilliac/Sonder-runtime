# WP1 Seventieth Slice: Shutdown Boundary Caller Migration

Status: implemented on `agent/wp1-execution-status`.

## Scope

The packaged HTTP lifecycle adapter now imports `ShutdownCoordinator` through
`sonder_runtime.platform.shutdown`, the canonical packaged platform boundary.
That platform module continues to re-export the existing root implementation,
so drain admission, cancellation, deadlines, interrupted hooks, flush hooks,
signal handling, and state transitions are unchanged. The root
`sonder_shutdown` module remains available for legacy callers and the
compatibility boundary itself.

This slice changes only the lifecycle caller and its focused boundary
regression. `server.py`, the command catalog, persistence, launchers, HTTP
serving/REPL interfaces, and `strangler_services.py` are unchanged.

## Evidence

- Packaged lifecycle boundary regression and shutdown tests pass.
- `python -m compileall -q sonder_runtime server.py`: passes.
- `scripts/check_architecture.py`: passes.
- `scripts/check_requirement_evidence.py`: passes.
- `git diff --cached --check` and `git diff --check`: pass.

## Remaining boundary

`sonder_runtime.platform.shutdown` still re-exports the root implementation.
The root `sonder_shutdown` allowance cannot be removed until the implementation
and its remaining legacy/test callers are migrated without changing shutdown
semantics.
