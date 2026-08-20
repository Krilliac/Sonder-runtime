# WP1 Thirty-Sixth Slice: Checklist Formatting Adapter

Status: implemented on `agent/wp1-execution-status`.

## Scope

The pure checklist renderer moved from the server composition root to
`sonder_runtime.adapters.task_formatting`. The server retains its public
callable for existing command and interface callers while the task presentation
logic has one canonical owner.

## Evidence

- Task-application, task-HTTP-scope, server-helper, and workbench regressions:
  **245 passed**.
- `python -m compileall -q sonder_runtime server.py`: passes.
- `scripts/check_architecture.py`: passes.
- `scripts/check_requirement_evidence.py`: passes.
- `git diff --cached --check`: passes.

## Remaining boundary

Checklist persistence and command orchestration remain in their existing
application and server owners; this slice moves only the pure renderer.
