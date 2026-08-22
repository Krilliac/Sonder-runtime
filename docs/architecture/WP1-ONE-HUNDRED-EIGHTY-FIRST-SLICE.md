# WP1 One-Hundred-Eighty-First Slice: Master Timeout Boundary

## Boundary

Moved the pure master-orchestration timeout normalization policy from the
root `server` module into `sonder_runtime.domain.master_timeout`. The root
module retains its compatibility delegate so existing orchestration call
sites continue to use the live runtime ceiling and named environment option.
This slice is limited to that policy and does not alter prior migration
boundaries or orchestration behavior.

## Evidence

- `tests/test_master_timeout.py` verifies packaged ownership, compatibility
  identity, default handling, and lower/upper bound behavior.
- `python -m pytest -q tests/test_master_timeout.py` passes.
- `python scripts/check_architecture.py` passes with zero violations.
- `python scripts/check_requirement_evidence.py` passes.
- `python -m compileall -q sonder_runtime server.py` passes.
- `git diff --check` passes.
