# WP1 Two-Hundred-Thirty-Fifth Slice — speculative-tool safety ownership

## Boundary

Moved the pure speculative-tool safety allowlist and membership helper from
root `sonder_speculation.py` into the packaged
`sonder_runtime.domain.speculation_policy` boundary. The root
`SPECULATABLE_TOOLS` name remains an identity-preserving compatibility alias,
and `BranchPredictor.speculatable()` remains the orchestration-facing seam.
Speculation scheduling, persistence, environment knobs, and dispatch remain
owned by `sonder_speculation.py` and are outside this slice.

## Verification

- `pytest -q tests/test_speculation_policy.py tests/test_speculation.py`
- `python scripts/check_architecture.py`
- `python scripts/check_requirement_evidence.py`
- `python -m compileall -q sonder_runtime sonder_speculation.py`
- `git diff --check`
