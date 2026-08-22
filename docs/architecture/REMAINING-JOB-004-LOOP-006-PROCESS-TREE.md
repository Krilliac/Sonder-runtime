# JOB-004 / LOOP-006 — Process-tree cleanup adapter

## Scope

This slice supplies the missing platform adapter behind the existing
`ProcessTreeCleanupContract`. It closes the actionable process-cleanup portion
of JOB-004 and the provider/process cleanup conformance portion of LOOP-006.
It does not claim that every execution provider is wired, and it does not edit
the formal master checklist or the requirement audit.

## Contract

`ProcessTreeSupervisor` accepts only a typed `ProcessTreeCleanupRequest` and
returns a `ProcessTreeCleanupReceipt`. Windows delegates to the OS-owned
`taskkill /T /F` tree operation. POSIX requires an explicit process group and
uses `killpg`; a bare pid is rejected because it cannot prove descendant
termination. Unsupported platforms, failed commands, and incomplete OS
responses are reported as incomplete rather than being presented as cleanup.

The adapter is compatible with `DurableJobRegistry.reconcile_with_cleanup` and
the existing `DurableLoopControl` cleanup evidence path. Its subprocess and OS
dependencies are injectable for deterministic tests.

## Evidence

- `tests/test_process_tree_supervisor.py` covers POSIX group enforcement,
  already-exited groups, Windows tree invocation, unsupported platforms, and
  type validation.
- Existing `tests/test_remaining_graceful_drain.py`,
  `tests/test_remaining_job_registry.py`, and
  `tests/test_remaining_loop_control.py` cover the application-level intent,
  reconciliation, and cancellation conformance that feed this adapter.

Formal checkboxes and audit classifications remain unchanged.
