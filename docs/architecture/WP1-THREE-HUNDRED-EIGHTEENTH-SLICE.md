# WP1 Three-Hundred-Eighteenth Slice — fanout failure classification

## Boundary

The fanout transport-failure helpers (`_fanout_failure_class`,
`_fanout_safe_error`, `_fanout_no_eligible_models_error`) now live in
`sonder_runtime/adapters/fanout_failures.py` as `failure_class`,
`safe_error` and `no_eligible_models_error`, with the closed receipt enum,
the content-free rendering and the skip-reason summary unchanged. They read
and construct the transport's `ModelCallError`, which is defined in the
adapters layer, so the adapters layer is their home; the prompt-echo pass
imports the packaged `fanout_redaction` directly. `server.py` keeps the three
root names as identity-preserving alias imports, so the fanout worker and
planner call the same objects.

## Evidence

- `tests/test_fanout_failures_boundary.py` verifies the alias identities, the closed failure-class table including HTTP statuses and foreign exceptions, the class-and-status-only safe rendering, and the name-free skip summary with its cooldown hint.
- `python -m pytest -q tests/test_fanout_failures_boundary.py tests/test_model_fanout.py`
- `python scripts/check_architecture.py`
- `python scripts/check_error_signals.py`
- `python -m compileall -q sonder_runtime server.py`
- `git diff --check`
