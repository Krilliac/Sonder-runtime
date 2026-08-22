# WP1 Thirty-First Slice: Trace Buffer Adapter

Status: implemented on `agent/wp1-execution-status`.

## Scope

The server composition root no longer owns bounded turn-trace storage or trace
rendering. The implementation now lives in
`sonder_runtime.adapters.observability.trace_buffer`; the server imports the
shared deque and compatibility callables so existing inspection and test
surfaces retain their behavior.

## Evidence

- Server helper, trace, and request-cache regressions: **270 passed**.
- `python -m compileall -q sonder_runtime server.py`: passes.
- `scripts/check_architecture.py`: passes.
- `scripts/check_requirement_evidence.py`: passes.
- `git diff --cached --check`: passes.

## Remaining boundary

The server remains a large composition root. Diagnostic state ownership is now
explicitly separated from command orchestration; additional extractions must
continue to preserve the direct-call compatibility surface and dependency
direction.
