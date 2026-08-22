# WP1 One-Hundred-Seventy-Ninth Slice: Local Model Options Boundary

## Boundary

Moved the local Ollama runtime-option policy out of root `server.py` and into
`sonder_runtime.platform.environment_options`. The root server retains a
compatibility delegate that injects the domain context normalizer and live
process environment, preserving existing request construction and callers.

This slice is limited to the remaining `_local_model_options` ownership and
does not alter the boundaries completed through slice 178.

## Evidence

- `tests/test_environment_options.py` verifies packaged ownership, injected
  context normalization, live environment option parsing, explicit GPU pins,
  and the server compatibility path.
- Existing server helper tests continue to exercise local request options
  through the compatibility delegate.
- `python scripts/check_architecture.py` passes with zero violations.
- `python scripts/check_requirement_evidence.py` passes.
- `python -m compileall -q sonder_runtime server.py` passes.
- `git diff --check` passes.
