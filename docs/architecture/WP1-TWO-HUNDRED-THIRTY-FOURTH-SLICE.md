# WP1 Two-Hundred-Thirty-Fourth Slice — process cancellation ownership

## Boundary

Moved the cooperative `CancellationToken` implementation from the packaged
shutdown coordinator into `sonder_runtime.platform.process`, where its
process-wide lifecycle role is explicit. `sonder_runtime.platform.shutdown`
continues to re-export the token for packaged callers, and
`sonder_shutdown.CancellationToken` remains an identity-preserving root
compatibility alias. Shutdown coordination, signal handling, drain deadlines,
and process-state transitions are unchanged.

## Evidence

- `tests/test_shutdown_boundary.py` verifies packaged process ownership and
  root/shutdown alias identity.
- `python -m pytest -q tests/test_shutdown_boundary.py tests/production/test_shutdown.py`
  passes.
- `python scripts/check_architecture.py` passes with zero violations.
- `python scripts/check_requirement_evidence.py` passes.
- `python -m compileall -q sonder_runtime sonder_shutdown.py` passes.
- `git diff --check` passes.
