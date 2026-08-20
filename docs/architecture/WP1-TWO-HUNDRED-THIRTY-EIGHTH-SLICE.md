# WP1 Two-Hundred-Thirty-Eighth Slice — context-health formatting boundary

## Boundary

Moved ownership of the pure context-health text formatter from the generic
`sonder_runtime.adapters.context_formatting` module to the canonical
`sonder_runtime.adapters.observability.health_formatting` boundary. The old
packaged import remains an identity-preserving compatibility alias, and
`server.context_health` keeps the same output and data-collection behavior.

The root `sonder_health` and packaged `sonder_runtime.domain.launcher_health`
nonce/HMAC contract are explicitly outside this slice. The existing pure
`health_bar` domain helper is also unchanged.

## Evidence

- `tests/test_health_formatting.py` verifies canonical ownership and the old
  packaged alias identity while retaining the existing meter contract tests.
- `python -m pytest -q tests/test_health_formatting.py tests/test_server_helpers.py -k context_health`
  passes.
- `python scripts/check_architecture.py` passes with zero violations.
- `python scripts/check_requirement_evidence.py` passes.
- `python -m compileall -q sonder_runtime server.py sonder_health.py` passes.
- `git diff --check` passes.
