# WP1 Two-Hundred-Eighth Slice — system-profile float policy ownership

## Boundary

Moved the verified non-negative floating-point environment override policy
used by `sonder_runtime.platform.system_profile` into the existing packaged
`sonder_runtime.platform.config_environment` boundary.  The system-profile
module retains the `_env_float` import alias, preserving its existing private
callers while making configuration-environment policy the implementation
owner.  This slice is limited to the platform boundary and does not modify
`server.py` or the adjacent slice-207 transport boundary.

## Evidence

- `tests/test_config_environment_ownership.py` verifies identity-preserving
  ownership plus default, whitespace, negative-value, and malformed-value
  behavior for the float policy.
- `python -m pytest tests/test_config_environment_ownership.py
  tests/test_system_profile_ownership.py -q` passes.
- `python scripts/check_architecture.py` passes with zero violations.
- `python scripts/check_requirement_evidence.py` passes.
- `python -m compileall -q sonder_runtime system_profile.py` passes.
- `git diff --check` passes.
