# WP1 Two-Hundred-Fourth Slice

## Boundary

Process-tree teardown for bounded toolchain probes now lives in the packaged
`sonder_runtime.adapters.process_termination` adapter. The legacy
`toolchain_status._terminate_process_tree` symbol remains a compatibility
delegate, so existing callers and test seams retain their behavior while OS
process ownership is moved into the adapter layer.

## Evidence

- `tests/test_process_termination_adapter.py` verifies Windows task-tree
  termination, POSIX process-group termination, fallback killing, and the
  already-finished fast path.
- `python scripts/check_architecture.py` passes.
- `python scripts/check_requirement_evidence.py` passes.
- `python -m compileall -q sonder_runtime server.py` passes.
- `git diff --check` passes.
