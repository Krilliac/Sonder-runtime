# WP1 Two-Hundred-First Slice

## Boundary

The bounded local model retry configuration and exponential backoff policy
now live in `sonder_runtime.platform.local_retry_policy`. The packaged owner
reads the historical `SONDER_LOCAL_RETRIES` and
`SONDER_LOCAL_RETRY_DELAY_MS` settings, applies safe limits, and keeps retry
delays within one second.

The root `server.py` helpers remain compatibility wrappers so existing model
transport behavior and callers continue to work while ownership moves out of
the legacy module.

## Evidence

- `tests/test_local_retry_policy_ownership.py` verifies packaged ownership,
  compatibility behavior, defaults, invalid values, bounds, and backoff.
- `tests/test_model_retry.py` covers the model transport retry integration.
- `python scripts/check_architecture.py` passes.
- `python scripts/check_requirement_evidence.py` passes.
- `python -m compileall -q sonder_runtime server.py` passes.
- `git diff --check` passes.
