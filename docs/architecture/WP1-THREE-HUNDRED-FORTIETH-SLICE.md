# WP1 Three-Hundred-Fortieth Slice — execution route formatting

## Boundary

Moved `_execution_route_header` from server.py into
`sonder_runtime/domain/execution_route_formatting.py` as
`execution_route_header`.

The original referenced the module-global `TIERS` dict and
`runtime_policy.LOCAL_TIERS` (platform layer). The packaged version
parameterizes both as `tiers_map` and `local_tiers` keyword arguments,
keeping the domain layer free of platform imports.

The root `server._execution_route_header` remains as a compatibility
delegate that passes `TIERS` and `runtime_policy.LOCAL_TIERS` through.
Not identity-preserving (delegate, not alias) because the module-global
binding requires the wrapper.

## Evidence

- `tests/test_execution_route_formatting_boundary.py` verifies header
  output, mode labels, confidence formatting, tier visibility, unmapped
  tier placeholder, and the server.py delegate.
- `python -m pytest -q tests/test_execution_route_formatting_boundary.py` — 8 passed
- `python scripts/check_architecture.py` — silent, exit 0
- `python scripts/check_requirement_evidence.py` — silent, exit 0
- `python scripts/check_error_signals.py` — silent, exit 0
- `python -m compileall -q sonder_runtime/domain/execution_route_formatting.py server.py` — silent, exit 0
- `git diff --check` — silent, exit 0
