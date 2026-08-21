# WP1 Two-Hundred-Fifty-Third Slice — cloud opt-in policy boundary

## Boundary

Moved the pure `SONDER_ALLOW_CLOUD` interpretation from `server.py` into
`sonder_runtime.domain.cloud_access.cloud_allowed`. The root keeps the
environment-reading compatibility wrapper; tier discovery, routing, and
provider calls remain unchanged.

## Evidence

- `tests/test_cloud_access_policy.py` preserves the existing message and
  identity checks and adds explicit true-value/false-value coverage.
- Server helper and cloud-routing regressions pass.
- Focused result: **220 passed**.
- Architecture and diff checks pass.
