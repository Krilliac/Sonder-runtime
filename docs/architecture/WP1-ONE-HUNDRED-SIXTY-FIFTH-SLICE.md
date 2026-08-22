# WP1 One-Hundred-Sixty-Fifth Slice: CPU Thread Default Policy

## Boundary

Moved the pure host CPU-thread default policy used by local model request
options from root `server.py` into the canonical platform environment-options
module. `server.py` retains an identity-preserving compatibility alias, so
existing callers and live option construction keep the same behavior.

This slice is limited to the CPU-thread policy. It does not alter the model
request transport, environment parsing rules, or any prior migration boundary.

## Evidence

- `tests/test_environment_options.py` verifies the single-thread minimum,
  fallback behavior, and root compatibility alias.
- `python -m pytest tests/test_environment_options.py -q` passes.
- `python scripts/check_architecture.py` passes with zero violations.
- `python scripts/check_requirement_evidence.py` passes.
- `python -m compileall -q sonder_runtime server.py` passes.
- `git diff --check` passes.
