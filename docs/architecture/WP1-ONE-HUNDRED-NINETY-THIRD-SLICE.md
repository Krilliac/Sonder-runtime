# WP1 One-Hundred-Ninety-Third Slice: Context Selection Adapter

## Boundary

Moved the remaining root `server.py` context-size selection helpers into the
packaged `sonder_runtime.platform.context_selection` adapter. The root helpers
remain compatibility delegates and retain the server session-context default;
the existing `sonder_runtime.platform.context_policy` ownership is unchanged.

## Evidence

- `tests/test_context_selection_adapter.py` verifies the packaged ownership,
  server-default handling, native clamping, and environment-backed defaults.
- `python scripts/check_architecture.py` passes with zero violations.
- `python scripts/check_requirement_evidence.py` passes.
- `python -m compileall -q sonder_runtime server.py` passes.
- `git diff --check` passes.
