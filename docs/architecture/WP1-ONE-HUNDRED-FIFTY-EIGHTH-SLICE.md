# WP1 One-Hundred-Fifty-Eighth Slice — toolchain probe policy ownership

## Boundary

Moved the fixed tool allowlist, argument policy, name normalization, and
canonical discovery lookup used by `toolchain_status.py` into the platform
boundary at `sonder_runtime.platform.toolchain_policy`. Process execution and
its output/timeout enforcement remain in the existing adapter. No server
surface or slice-157 boundary was changed.

## Verification

- `python -m pytest tests/test_toolchain_policy_ownership.py tests/test_toolchain_status.py -q` — pass.
- `python scripts/check_architecture.py` — pass.
- `python scripts/check_requirement_evidence.py` — pass.
- `python -m compileall -q sonder_runtime toolchain_status.py` — pass.
- `git diff --check` — pass.
