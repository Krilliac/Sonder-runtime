# Generic job live integration — 2026-08-21

## Scope

This is a bounded production-composition slice for the existing typed generic
job contracts. The application job service now exposes the richer durable
start/poll/stream/output/recovery vocabulary when the backing registry
supports it. The composition root injects the typed process-tree cleanup
contract into the live job service. The application recovery result is owned by
the application jobs boundary; SQLite remains an adapter.

Output writes preserve the registry's monotonic watermark, bounded retention,
truncation, and persisted `SpillReference`. Newly appended output is forwarded
through the existing idempotent job/session linkage adapter. Restart recovery
uses the existing bounded drain plan and marks an orphan interrupted only after
the cleanup receipt is complete.

The missing durable-output import boundary exposed during process-provider test
collection was corrected by keeping the typed recovery report in
`sonder_runtime/application/jobs/durable_registry.py`; the persistence adapter
imports that contract rather than making application code import SQLite types.
The existing `sonder_runtime/adapters/execution/durable_output.py` remains the
concrete spill implementation.

## Evidence

Focused command:

```text
python -m pytest -q tests/test_generic_job_live_integration.py tests/test_job004_process_provider.py tests/test_exec004_durable_output.py tests/test_remaining_agent_005_job_integration.py tests/test_composition_job_registry.py
20 passed
```

Collection-only verification for the process and spill modules collected all
10 tests successfully. The live integration tests cover composition-root
start/poll, watermarked spill output and session linkage, and restart cleanup
with a complete receipt. Existing focused tests cover process-tree cleanup,
SQLite reopen/recovery, bounded output truncation, and spill integrity.

## Limitations

This remains `implemented_unverified`: it does not promote the master
specification, run the full repository suite, or claim provider security,
automatic generic-job scheduling, or silent restart resumption. Process
provider implementation files and session repository implementation files are
outside this slice. The Windows process-tree adapter still reports the truth
returned by `taskkill`; production deployment receipts remain outstanding.
