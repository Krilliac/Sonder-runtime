# WP1 Two-Hundred-Fifteenth Slice — live cloud-tier repair ownership

## Boundary

Moved the pure live-reload repair policy for the retired `cloud-general`
binding from root `server.py` into
`sonder_runtime.domain.cloud_tier_policy`. The root server retains
`_refresh_live_cloud_tiers()` as a compatibility wrapper and remains the
composition root that supplies the mutable tier map, process environment, and
typed runtime default.

## Evidence

- `tests/test_cloud_tier_policy.py` verifies exact legacy matching, the
  operator preservation flag, non-legacy bindings, and missing bindings.
- Existing `tests/test_server_helpers.py` coverage verifies the compatibility
  wrapper and typed projection behavior.
- `python -m pytest tests/test_cloud_tier_policy.py tests/test_server_helpers.py -q`
  passes.
- `python scripts/check_architecture.py` passes with zero violations.
- `python scripts/check_requirement_evidence.py` passes.
- `python -m compileall -q sonder_runtime server.py` passes.
- `git diff --check` passes.
