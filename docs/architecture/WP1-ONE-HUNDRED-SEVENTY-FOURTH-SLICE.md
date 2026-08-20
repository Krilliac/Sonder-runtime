# WP1 One-Hundred-Seventy-Fourth Slice: Context Policy Platform Boundary

## Boundary

Moved the verified environment-backed native/virtual context sizing policy from
the root `context_policy` module into
`sonder_runtime.platform.context_policy`. The root module now resolves to the
canonical platform module, preserving legacy import identity and live
environment behavior. `server.py` and the model-sizing boundary remain
untouched.

## Evidence

- `tests/test_context_policy_platform_boundary.py` verifies canonical module
  identity, live KV-cache default selection, and native/virtual clamping.
- Existing `tests/test_context_policy.py` continues to cover parsing, strict
  validation, environment overrides, and formatting through the compatibility
  import.
- `python scripts/check_architecture.py` passes with zero violations.
- `python scripts/check_requirement_evidence.py` passes.
- `python -m compileall -q sonder_runtime server.py` passes.
- `git diff --check` passes.
