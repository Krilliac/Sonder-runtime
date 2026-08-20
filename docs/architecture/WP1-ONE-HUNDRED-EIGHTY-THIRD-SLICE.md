# WP1 One-Hundred-Eighty-Third Slice: Chat Code-Gate Target Boundary

## Boundary

Moved the pure chat code-gate target-selection policy from root `server.py`
into `sonder_runtime.domain.code_gate_policy`. The root server retains a
compatibility delegate and injects the legacy grounding extractor at the
runtime boundary. This slice is limited to target selection; code execution,
repair, and outcome recording remain in the existing application path.

## Evidence

- `tests/test_code_gate_policy.py` verifies packaged ownership, definition and
  import selection, trivial/non-Python rejection, interactive-input rejection,
  and the root compatibility delegate.
- Existing `tests/test_code_gate.py` continues to cover the consuming gate
  behavior through `server._code_gate_target`.
- `python scripts/check_architecture.py` passes with zero violations.
- `python scripts/check_requirement_evidence.py` passes.
- `python -m compileall -q sonder_runtime server.py` passes.
- `git diff --check` passes.
