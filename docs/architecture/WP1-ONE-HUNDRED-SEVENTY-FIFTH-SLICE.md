# WP1 One-Hundred-Seventy-Fifth Slice: Context Character-Count Estimator Boundary

## Boundary

Moved the pure character-count token-estimation helper used by the root
`server.py` context-health path into the canonical
`sonder_runtime.domain.context_formatting` module. The server retains its
`_rough_token_count_from_chars` compatibility alias, while the domain module
owns both text and pre-counted context estimation policies.

## Evidence

- `tests/test_context_formatting.py` verifies canonical ownership, the server
  compatibility alias, empty/short inputs, and negative-value clamping.
- `python scripts/check_architecture.py` passes with zero violations.
- `python scripts/check_requirement_evidence.py` passes.
- `python -m compileall -q sonder_runtime server.py` passes.
- `git diff --check` passes.
