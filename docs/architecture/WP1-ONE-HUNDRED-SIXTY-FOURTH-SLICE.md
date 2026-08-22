# WP1 One-Hundred-Sixty-Fourth Slice: Repository Error Translation Adapter

Status: implemented on `agent/wp1-execution-status`.

## Scope

The concrete storage-error translation seam previously owned by
`sonder_runtime.adapters.task_repository` now lives in
`sonder_runtime.adapters.repository_errors`. The task repository delegates to
that adapter and retains its private `_store_call` compatibility alias.

This slice does not change task persistence operations, domain error codes, or
the root compatibility surface. It does not touch `server.py` or the slice-163
boundary.

## Evidence

- `tests/test_repository_error_adapter.py` verifies canonical ownership,
  success passthrough, and translation of `ValueError`, SQLite, and OS errors.
- `python scripts/check_architecture.py` passes with zero violations.
- `python scripts/check_requirement_evidence.py` passes.
- Focused tests, compile validation, and `git diff --check` pass.
