# JOB-004 — Concrete process-provider integration

## Scope

This bounded slice connects `SubprocessJobProvider` to the typed
`ProcessTreeSupervisor` contract through `DurableJobRegistry` and
`JobRegistryService.cancel_with_cleanup`. It covers one argv-based execution
provider path only; other child providers and retry-loop paths remain outside
this change.

## Implementation

- `ProcessJobRequest` validates argv, environment, and descendant bounds.
- `SubprocessJobProvider.start` launches the process, records its PID in the
  durable job registry, and creates a new POSIX process group. Windows keeps
  the PID as the OS-owned `taskkill /T` identity and does not invent a group.
- `cancel` uses the typed job cancellation service, so every cleanup request
  carries the registered process identity and the caller's bound/reason.
- The provider removes its live-process mapping only after a complete cleanup
  receipt. Incomplete receipts are returned unchanged and remain available for
  follow-up/reconciliation.
- `wait` publishes success or non-zero process exit truth and never converts a
  timeout into a terminal result.

## Evidence

Focused tests:

```text
python -m pytest -q tests/test_job004_process_provider.py
6 passed
```

The tests prove process identity registration, POSIX group setup, cleanup
request bounds, cancellation, complete cleanup handling, incomplete-cleanup
reporting, and terminal wait status. No filesystem, HTTP, MCP, session, tool
audit, memory, training, data, evaluation, update, operations, model,
compaction, or selfmod files were changed for this slice.
