# WP1 One-Hundred-Eighty-Fourth Slice

## Boundary

Moved the pure artifact execution-risk denial policy from the root
`artifact_risk.py` implementation into the packaged domain module
`sonder_runtime.domain.artifact_risk_policy`. The root module retains the
canonical import as a compatibility export for existing callers.

## Evidence

- `tests/test_artifact_risk_policy.py` verifies domain ownership, each denial
  threshold, permissive policies, and the root compatibility identity.
- `python scripts/check_architecture.py` passes.
- `python scripts/check_requirement_evidence.py` passes.
- `python -m compileall -q sonder_runtime artifact_risk.py` passes.
- `git diff --check` passes.

## Scope

This slice does not modify `server.py`, the slice-183 boundary, or the
artifact inspection and enforcement adapter behavior.
