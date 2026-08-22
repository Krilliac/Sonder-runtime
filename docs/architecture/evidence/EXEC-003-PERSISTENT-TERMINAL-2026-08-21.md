# EXEC-003 persistent terminal adapter evidence — 2026-08-21

## Scope

This slice wires the existing typed `TerminalService` and
`TerminalHandle` contracts to `SQLitePersistentTerminalService` in
`sonder_runtime/adapters/execution/persistent_terminal.py`. The adapter owns
the local subprocess and persists terminal identity, lifecycle state,
dimensions, and bounded output rows in SQLite. No HTTP, MCP, job/session,
audit, memory, training, data, agent, evaluation, update, operations, model,
compaction, or selfmod files are part of this slice.

## Guarantees demonstrated

- `open_named`, `send`, `resize`, `reconnect`, and `stop` operate through the
  typed terminal capability; handles remain non-owning.
- Output is persisted as monotonically numbered rows and exposed through the
  existing `OutputWatermark`/`OutputPage` types. Reads enforce both event and
  byte bounds, report `has_more`, and report `truncated` when retention has
  removed the requested prefix.
- A second service instance can replay stopped durable output, but cannot
  impersonate a live process. Reconnect without the adapter-owned process
  fails closed and marks the durable session stopped.
- Cleanup first requests process termination and reports non-quiescent when
  the deadline cannot prove exit. It reports quiescent only after all owned
  processes have exited.

## Verification

`tests/test_exec003_persistent_terminal.py` covers reconnect and durable
replay, bounded reads and watermark gaps, fail-closed cleanup, and orphaned
owner handling. The focused suite passed with `python -m pytest -q
tests/test_exec003_persistent_terminal.py`.

The adapter is intentionally local-process based. A separately deployed or
remote terminal provider remains outside this bounded production slice and
must supply its own owner/reconnect evidence before being called verified.
