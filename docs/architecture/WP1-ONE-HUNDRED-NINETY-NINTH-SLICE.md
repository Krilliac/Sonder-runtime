# WP1 One-Hundred-Ninety-Ninth Slice: Reasoning Exposure Policy

## Boundary

Moved the root `server.py` environment policy that decides whether model
reasoning should be exposed into `sonder_runtime.platform.reasoning_policy`.
The root `server.reasoning_exposure_enabled` callable remains as a
compatibility delegate, while the private chain-of-thought permission gate and
reasoning transport remain outside this slice.

## Evidence

- `tests/test_reasoning_policy.py` covers the fail-closed default, accepted
  opt-in values, rejected values, and the root compatibility behavior.
- `python -m pytest tests/test_reasoning_policy.py -q` passes.
- `python scripts/check_architecture.py` passes with zero violations.
- `python scripts/check_requirement_evidence.py` passes.
- `python -m compileall -q sonder_runtime server.py` passes.
- `git diff --check` passes.
