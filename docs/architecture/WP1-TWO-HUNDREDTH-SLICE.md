# WP1 Two-Hundredth Slice — configuration environment scalar ownership

## Boundary

Moved the verified scalar compatibility-environment policy used by
`sonder_runtime.platform.config` into the dedicated
`sonder_runtime.platform.config_environment` boundary.  The packaged config
loader retains identity-preserving `_env_bool` and `_env_int` aliases so its
existing private test and compatibility surfaces remain stable.  The slice
does not modify `server.py` or the adjacent slice-199 boundary.

## Evidence

- `tests/test_config_environment_ownership.py` verifies packaged ownership,
  identity-preserving aliases, truthy spellings, default handling, and
  fail-closed malformed integer reporting.
- `python scripts/check_architecture.py` passes with zero violations.
- `python scripts/check_requirement_evidence.py` passes.
- `python -m compileall -q sonder_runtime sonder_config.py` passes.
- `git diff --check` passes.
