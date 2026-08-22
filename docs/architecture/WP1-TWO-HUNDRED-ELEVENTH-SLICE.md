# WP1 Two-Hundred-Eleventh Slice — context compaction payload ownership

## Boundary

Moved the pure context-overflow retry payload transformation from root
`server.py` into the existing packaged context-compaction domain boundary at
`sonder_runtime.domain.context.compaction`.  The root
`_compacted_overflow_payload` name remains as a compatibility delegate for
legacy callers.  The migration changes no retry behavior: only the message
list is replaced, while model options and all other payload fields are carried
through unchanged.

## Evidence

- `tests/test_context_compaction_boundary.py` verifies packaged ownership,
  conservative rejection, option preservation, and the root compatibility
  delegate.
- `python -m pytest tests/test_context_compaction_boundary.py
  tests/test_context_overflow.py -q` passes.
- `python scripts/check_architecture.py` passes with zero violations.
- `python scripts/check_requirement_evidence.py` passes.
- `python -m compileall -q sonder_runtime server.py` passes.
- `git diff --check` passes.
