# WP1 Two-Hundred-Fifty-Fourth Slice — timeout caller migration

## Boundary

Rewired all internal model HTTP/discovery callers in `server.py` to invoke
the packaged `sonder_runtime.domain.request_timeout.bound_request_timeout`
policy directly. The root `_bounded_timeout` function remains only as a
compatibility wrapper for external callers and tests.

## Evidence

- Source audit leaves one wrapper definition and no internal wrapper callers.
- `tests/test_request_timeout_policy.py`, server helpers, and model-retry
  regressions: **255 passed**.
- Architecture, compile, and diff checks pass.
