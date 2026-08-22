# WP1 One-Hundred-Ninety-First Slice

## Boundary

The pure local-runtime summary projection now lives in
`sonder_runtime.platform.runtime_summary.local_runtime_summary`. It owns the
stable public field names and Ollama-default fallbacks while receiving the
already-computed options and requested context from the caller.

Root `server._local_runtime_summary` remains a compatibility delegate. The
runtime option builder and context policy remain in their previously migrated
owners; this slice changes only summary projection ownership.

## Evidence

- `tests/test_runtime_summary.py` verifies complete projection, default
  fallbacks, and root compatibility behavior.
- `python scripts/check_architecture.py` passes.
- `python scripts/check_requirement_evidence.py` passes.
- `python -m compileall -q sonder_runtime server.py` passes.
- `git diff --check` passes.
