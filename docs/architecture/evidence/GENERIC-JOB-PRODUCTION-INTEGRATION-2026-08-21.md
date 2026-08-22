# Generic-job production integration slice

Date: 2026-08-21

## Scope

This bounded slice wires the existing typed durable-job contracts into the
application composition root. It does not alter session or provider lifecycle
implementation files.

The application now lazily composes:

- the SQLite durable job registry and existing job/session lifecycle adapter;
- a typed process provider using the shared process-tree supervisor;
- a durable SQLite spill store for completed process output;
- a job recovery callback, which runs bounded startup reconciliation through
  the same process-tree cleanup contract.

Completed process output is appended to the durable job output stream. Output
within the inline bound remains in the registry; larger output stores a
durable spill reference while retaining a bounded preview. Poll and stream
operations continue to use immutable records and monotonic watermarks.

## Evidence

Focused verification:

    python -m compileall -q sonder_runtime
    pytest -q tests/test_generic_job_composition.py
    pytest -q tests/test_composition_job_registry.py tests/test_job004_process_provider.py tests/test_job_session_lifecycle.py tests/test_exec004_durable_output.py tests/test_remaining_agent_005_job_integration.py

The focused suite passed. The composition test proves provider caching,
SQLite-backed start/poll, restart visibility, bounded stream watermarks,
durable spill references, and pending-job recovery classification. Existing
process-provider, lifecycle, spill, registry-reopen, and cleanup tests remain
green.

## Limitations

The process provider still owns an in-process map of live subprocess handles;
after a process-runtime restart, recovery can classify durable records and
request cleanup, but it cannot reconstruct a provider wait handle. The
composition root exposes typed provider/recovery callbacks; no new HTTP
surface was added because the existing HTTP job start/read/stream routes
already read the composed registry. Full repository regression and production
OS-level taskkill receipt verification remain outstanding.
