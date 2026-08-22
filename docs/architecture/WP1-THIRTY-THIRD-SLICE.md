# WP1 Thirty-Third Slice: Lesson-ID Validation Adapter

Status: implemented on `agent/wp1-execution-status`.

## Scope

The bounded lesson-ID parser used by memory-review commands moved from the
server composition root to `sonder_runtime.adapters.memory_lesson_ids`. The
server preserves the existing callable while the canonical validation logic is
now owned by a memory adapter.

## Evidence

- Memory-quality, memory-tool, and server-helper regressions: **235 passed**.
- `python -m compileall -q sonder_runtime server.py`: passes.
- `scripts/check_architecture.py`: passes.
- `scripts/check_requirement_evidence.py`: passes.
- `git diff --cached --check`: passes.

## Remaining boundary

The server still owns the larger memory command orchestration and model/tool
composition. This extraction removes only the pure bounded parser and keeps
the transport-facing compatibility surface stable.
