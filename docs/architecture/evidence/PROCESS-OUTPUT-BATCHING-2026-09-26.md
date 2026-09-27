# Batched process-output persistence — 2026-09-26

## Problem

`SubprocessJobProvider` published every line a child printed as its own
durable-registry commit. On Linux that cost about 1.5 ms a line. On the hosted
`windows-latest` runner it cost about 60 ms a line (CI run 36273300357, the
Windows-focused job). Output only counts once it reaches the registry. So a
flooding debugger, profiler or structured test run hit its step deadline
before it reached the 16 MiB output limit. The debug launcher's output-limit
test had to drop its limit from 4 MiB to 512 KiB to pass on Windows
(commit b63106f1). The same test now runs at 4 MiB again.

## Design

The provider now has one persister thread per job, beside the stdout and
stderr reader threads. All three threads come from
`platform.runtime_threads`. Readers hand each line to an `OutputBatcher`
(`sonder_runtime/adapters/execution/output_batching.py`). The persister
publishes whatever has built up through one `append_outputs` call, which is
one registry transaction.

A batch is flushed when the first of these happens:

| Trigger | Default |
|---|---|
| Line bound: `OutputBatchPolicy.max_lines` | 1024 lines |
| Byte bound: `OutputBatchPolicy.max_bytes` | 256 KiB (UTF-8) |
| Time bound: the oldest pending line has waited `max_delay_seconds` | 50 ms |
| Every reader reached end of file (the child exited or closed its pipes) | immediate |
| Cancellation (`cancel` requests a flush and never waits for it) | immediate |
| `wait`, after the root exits and readers are joined (bounded by `OUTPUT_DRAIN_SECONDS`, 5 s) | immediate |

The policy is typed configuration: pass `output_batch=` to
`SubprocessJobProvider`. The defaults are module constants. How the defaults
were measured on Linux, with one commit on the SQLite registry:

| Lines per commit | Cost per commit | Cost per line |
|---|---|---|
| 1 | 1.5 ms | 1.5 ms |
| 256 | 1.9–2.5 ms | 0.008 ms |
| 1024 | 2.5–3.8 ms | 0.003 ms |
| 4096 | 5.5–6.2 ms | 0.0015 ms |

A commit costs about the same whatever it carries, so 1024 lines per batch
lowers the cost per line by two to three orders of magnitude. One transaction
stays well under the registry's `MAX_OUTPUT_APPEND_BATCH` (4096 entries).

### What does not change

- **Events.** Each line is still its own event, with its own sequence number,
  stream and exact text. That includes a partial last line and the effect of
  universal newlines (CRLF and a lone CR end a line). Invalid UTF-8 still
  ends the text-mode reader quietly. Oversized lines still spill one at a time.
- **Retention.** `append_outputs` gives the same retained tail, `output_next`
  and `output_dropped_before` as appending the same lines one at a time.
  Retention only ever drops a prefix, so trimming once per batch reaches the
  same state. Entries that would be dropped inside the batch are not written.
- **Output-limit accounting.** The debug launcher's counter reads registry
  events, which are unchanged. The limit trips at the same byte threshold. The
  launcher sees the crossing at batch granularity instead of line granularity.
- **Backpressure.** Readers block while the unpersisted window is full, so a
  child that outruns storage is still slowed down through its pipe.
- **Failures.** A persistence error stops the readers, as a failed per-line
  commit did. Classification is unchanged: `OSError` and `ValueError` end
  output quietly, and any other error fails the job with the exception type.
  If `wait` cannot publish the last window within `OUTPUT_DRAIN_SECONDS`, it
  fails closed: the job ends `FAILED` with
  `process output persistence failed (TimeoutError)` rather than `SUCCEEDED`
  with output still unpersisted.

### Crash-loss bound

Readers block while the pending batch plus the batch being committed holds
`max_lines` lines or `max_bytes` bytes. So at most `max_lines` lines and
`max_bytes` bytes (plus the one line that crossed the byte bound) are queued
without being durable. A reader blocked in `put` also holds one line it has
already read, so read-but-not-durable output is at most that window plus one
line per reader (stdout and stderr: two). By default that is 1024 lines or
256 KiB, plus two lines. No queued line waits longer than 50 ms plus one
commit.

A runtime crash loses at most that. Each batch commits atomically and in
order, so after a restart the registry holds an exact, gap-free prefix of
each stream as it was read. Nothing is duplicated or reordered. Recovery
(`reconcile_with_cleanup`) never replays output. New output continues from
the durable `output_next`.

## Registry API

`append_outputs(job_id, entries)` takes `OutputAppend(stream, data, spill)`
values. Every entry is validated before any is written, with the same stream
and text checks as `append_output`. The batch is limited to 4096 entries.

- The SQLite registry runs the batch as one transaction. `append_output` is
  now a one-entry batch.
- The in-memory registry holds its lock for the whole batch and appends
  through `append_output`, so subclasses that override the single-line path
  still apply.
- The provider falls back to per-line `append_output` for a registry without
  `append_outputs`.
- There is no PostgreSQL implementation of this registry.

## Evidence

The regression tests are in `tests/test_process_output_batching.py`. They
cover:

- **Throughput.** 20,000 lines through the real provider and SQLite registry
  must persist in under 10 s. Batched, they take about 0.25 s on Linux, or
  about 1.5 s with a simulated 60 ms commit. With the per-line provider and
  registry restored temporarily, the same test failed on Linux after 20.1 s
  (23.0 s on a re-run).
- **Batch bounds.** Flushes on the line, byte and time bounds, on end of file
  and on an explicit flush, plus the unpersisted-window bound under a stalled
  commit.
- **Equivalence with the per-line path.** Byte-exact output for CRLF, lone CR,
  blank lines, Unicode, an oversized line, stderr, a partial last line and
  invalid UTF-8. Registry state after every batch boundary equals line-at-a-time
  appends, for both registries and for several retention bounds. Both
  registries treat a batch as all-or-nothing, and SQLite runs it as one
  transaction.
- **Cancellation.** Cancelling during a 30 s window returns promptly,
  publishes the pending lines at once and leaves no process behind.
- **Output-limit accounting.** Batched and per-line appends give the same
  counted bytes.
- **Crash and restart.** The host process is killed with 20 lines unflushed.
  After restart the registry holds exactly lines 0–99 at sequences 1–100.
  Recovery interrupts the job and cleans up the child. Output after restart
  continues at sequence 101.

The debug launcher test
`test_a_flooding_step_is_stopped_at_the_output_limit_with_a_bounded_registry`
is back at a 4 MiB limit. It asserts that the step outcome is `output_limit`,
that the recorded cancel reason is `OUTPUT_LIMIT` and not the deadline, and
that the run finishes in under 20 s against the 60 s step deadline. It takes
about 1.6 s on Linux, or 2.5 s with a simulated 60 ms commit. With the
per-line provider restored and a 60 ms commit, the same test is stopped by
the deadline at 61 s, which reproduces the Windows CI failure.
