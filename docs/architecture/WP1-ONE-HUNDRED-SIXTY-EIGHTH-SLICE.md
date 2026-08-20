# WP1 One-Hundred-Sixty-Eighth Slice: Secret-Presence Policy Boundary

## Boundary

Moved the pure configuration secret-presence redaction policy from the
platform configuration implementation into the dedicated platform boundary at
`sonder_runtime.platform.secret_presence`. `sonder_runtime.platform.config`
continues to own configuration objects and delegates redacted output to the
platform policy, so no secret value is exposed and the existing configuration
contract remains unchanged.

## Evidence

- `tests/test_secret_presence_platform.py` covers set/unset markers, falsey
  inputs, and the configuration redacted-dict integration.
- `python -m pytest tests/test_secret_presence_domain.py tests/production/test_config.py -q`
  passes.
- `python scripts/check_architecture.py` passes with zero violations.
- `python scripts/check_requirement_evidence.py` passes.
- `python -m compileall -q sonder_runtime server.py` passes.
- `git diff --check` passes.
