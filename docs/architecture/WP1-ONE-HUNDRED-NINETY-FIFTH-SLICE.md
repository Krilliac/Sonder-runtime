# WP1 One-Hundred-Ninety-Fifth Slice

## Boundary

Moved the pure lesson-distillation timeout policy out of root `server.py`
into `sonder_runtime.domain.distillation_policy`. The root server retains a
compatibility wrapper because it owns the live request-timeout ceiling and
the platform environment-option reader.

## Evidence

- `tests/test_distillation_policy.py` verifies the default, lower bound, live
  ceiling, injected environment reader, and process-environment isolation.
- `python scripts/check_architecture.py` passes.
- `python scripts/check_requirement_evidence.py` passes.
- `python -m compileall -q sonder_runtime server.py` passes.
- `git diff --check` passes.

## Scope

This slice changes only the distillation timeout policy boundary and its
compatibility wrapper. It does not alter the distillation workflow, memory
repository, model gateway, or any previously migrated boundary.
