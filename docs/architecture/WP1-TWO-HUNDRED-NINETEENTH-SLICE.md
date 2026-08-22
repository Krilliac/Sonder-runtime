# WP1 Two-Hundred-Nineteenth Slice

## Boundary

Moved the pure clean-generation retrieval policy from root `server.py` into
`sonder_runtime.domain.retrieval_policy` as `no_retrieve`. The policy accepts
the retrieval hook inputs but deliberately returns no lessons for teacher/
clean generation, keeping local augmentation out of the captured output while
preserving the root `server._no_retrieve` compatibility wrapper.

## Evidence

- `tests/test_retrieval_policy.py` verifies ignored hook inputs, fresh return
  values, and the root compatibility wrapper.
- `python scripts/check_architecture.py` passes.
- `python scripts/check_requirement_evidence.py` passes.
