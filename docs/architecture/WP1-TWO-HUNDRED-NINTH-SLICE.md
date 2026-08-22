# WP1 Two-Hundred-Ninth Slice — hosted thinking budget policy ownership

## Boundary

Moved the pure hosted-model prediction-budget normalization policy from
`server.py` into `sonder_runtime.domain.cloud_thinking_budget`.  The root
server helper remains a compatibility delegate, preserving existing callers
while making the domain policy the implementation owner.  This slice is
limited to the request-budget policy and does not alter transport, model
selection, or other adjacent migration boundaries.

## Evidence

- `tests/test_cloud_thinking_budget.py` verifies packaged ownership, the
  minimum-budget adjustment, copy-on-write options behavior, and no-op cases.
- `python -m pytest tests/test_cloud_thinking_budget.py -q` passes.
- `python scripts/check_architecture.py` passes with zero violations.
- `python scripts/check_requirement_evidence.py` passes.
- `python -m compileall -q sonder_runtime server.py` passes.
- `git diff --check` passes.
