# Generic job live output — 2026-08-21

## Scope

The composed subprocess provider now publishes stdout and stderr incrementally
through the existing durable job-output registry. Real `Popen` pipes are owned
by bounded daemon reader threads; the provider's wait path waits on the process
and joins those readers instead of consuming the pipes a second time through
`communicate`. Lightweight process doubles retain the previous compatibility
path.

Each published chunk uses the existing monotonic output sequence, bounded
retention, inline preview, and optional spill-reference contract. A caller can
read a running job's first output watermark before the process reaches a
terminal state and resume from that watermark after completion.

## Evidence

Focused command:

```text
python -m pytest -q --basetemp .pytest-live-job-output tests/test_job004_process_provider.py tests/test_generic_job_live_integration.py
9 passed
```

`test_running_process_publishes_incremental_output_before_wait` launches the
current Python interpreter with unbuffered output, observes `first` while the
job is still non-terminal, waits for completion, and resumes from the prior
watermark to observe `second`.

## Limitations

This remains `implemented_unverified`: it does not claim a full deployment
matrix, a cross-process provider-handle reconstruction, or formal checklist
promotion. Reader threads are intentionally process-local and daemonized;
restart recovery continues to rely on the durable registry and cleanup
contract rather than silently resuming an orphan process.
