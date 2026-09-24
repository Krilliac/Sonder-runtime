# Bounded waits, worker progress, and interrupted publication

## Recovered evidence and limits

The operator requested continuation of an interrupted recovery conversation
and supplied the seven-item silent-lock/stalled-worker audit on 2026-09-24.
The conversation reports command-service disconnects and subsequent recovery.
Its last visible activity is reading Git blob contents during publication of a
reviewed effect-recovery fix. Publication had been performed through many
individual blob operations. The server's transport logs, pending RPC state and
worker stack are not exposed by that share.

The GitHub branch at `ce0291bf0d93f1aa52462e1d090f58bb535e57b7` retained the
four earlier continuation commits and green hosted checks. It did **not**
contain the final receipt-ordering and uncertainty-write fixes described in
the chat. The unresolved review identified completion before durable audit
publication. This establishes an interrupted publication boundary. It does
not establish a lock deadlock, PID reuse, model failure, or a particular
ChatGPT server/node as the root cause. Repeated small publication operations
increase the number of interruption points; that is a risk inference, not a
diagnosis of the underlying service.

## Implementation workload

| Item | Boundary and intended result | Regression evidence |
| --- | --- | --- |
| 1 | Fleet progress is independent of background owner heartbeat. Bounded coordinator waits surface stalled lanes and preserve ownership of unfinished workers. | `tests/test_adaptive_concurrency.py`, `tests/test_fleet_store.py` |
| 2 | Command-journal thread and OS lock acquisition has a deadline; launcher failures report a busy response and holder context. | `tests/test_command_recovery.py` |
| 3 | Shared advisory locking publishes diagnostic owner records, retains the lock inode, and names the holder on timeout. JSON, artifact, scenario, training and journal users share the primitive. | `tests/test_curriculum_store_durability.py`, lock and publication regression suites |
| 4 | Training, nightly, migration and fleet owners store process identity alongside PID; foreign or uninspectable owners are not proof of death. | `tests/test_process_wait_hardening.py`, `tests/test_fleet_store.py` |
| 5 | Legacy migration has bounded thread/OS/legacy-owner waits and reports the holder while waiting. | `tests/test_process_wait_hardening.py`, `tests/test_sonder_paths.py` |
| 6 | Selfmod start/status/stop use a recorded process instance, not command-line substring matches. | PowerShell real-child harness |
| 7 | Alias-transition refusals name creation time and policy; recovery remains owned by the original policy. | `tests/test_process_wait_hardening.py`, `tests/test_adaptive_training.py` |
| Receipt publication | Audit must be durable before definitive effect completion; uncertain writes fence admission even during storage failure. | `tests/test_tool_gateway_effect_crash_matrix.py`, `tests/test_effect_journal.py` |

Advisory lock metadata is diagnostic and is not termination authority. Locks
are not broken merely because a progress deadline expires. Generic threads
cannot be safely killed; cancellation is cooperative and their effects remain
uncertain until reconciled. A new request ID is not evidence that repeating an
uncertain operation is safe.

## Architecture decision

The lock implementation lives in the filesystem adapter, with `durable_locks`
retained as a compatibility alias. Database path initialization can occur
before application bootstrap. Its existing migration path therefore has two
exact allowed adapter dependencies: the common filesystem lock and the
non-destructive process-liveness probe. The architecture checker permits only
these two edges from `platform/paths.py`; adjacent platform modules and other
adapter imports remain rejected. A regression test pins that boundary.

## Operational recovery

The fleet progress window is configured with
`SONDER_FLEET_PROGRESS_DEADLINE_SECONDS` (120 seconds by default). Configure it
for the longest legitimate silent phase of the selected workload. A background
heartbeat is process liveness, not evidence that a model, tool or child made
progress. Read the stalled lane IDs and last activity before retrying.

Legacy migration waits are bounded at 30 seconds per admission stage. A
timeout leaves source data and active ownership intact. Inspect the named
process identity, host and start time; do not delete a live owner's lease or
the persistent advisory lock file to force progress.

Personal-alias transitions do not expire by wall time: they may represent
partly applied model/policy changes. Use `SONDER_RUNTIME_POLICY` to select the
reported owning policy, inspect `python adaptive_training.py status`, and run
`python adaptive_training.py rollback` through its normal lifecycle checks.
For incomplete evidence, preserve the journal and transition for manual
inspection. Never clear a foreign policy's transition solely because it is old.

The recovered branch is updated through one ordinary Git push after tests,
then checked at the exact remote SHA. No live Sonder deployment is part of
this recovery. Existing issue #515 child-checkpoint coupling and #517
independent evaluation/promotion requirements remain open until separately
qualified.

## Validation status

Integration review reproduced a POSIX directory-swap case in which diagnostic
owner metadata followed a replaced path instead of the locked descriptor's
directory. Metadata operations now use the same directory descriptor; a real
WSL POSIX probe observed the escape before the fix and confinement afterward.
The committed regression runs on POSIX; Windows directory anchoring already
denies rename. Definitive outcome replay also releases an outage fence only
after the entire run is durably settled; a pending sibling retains the fence.

The first full Windows run stopped after 13 failures, 4,650 passes and seven
skips. It exposed missing binary mode in strategy-key file descriptors, two
timing-sensitive expectations, and Windows path-normalization assumptions in
Codegen test fixtures. Key reads/writes now preserve all byte values; startup
time remains part of worker budgets; production Windows build-isolation refusal
is retained. Those focused regressions pass. Final full-suite qualification is
still required.

Focused Windows process/migration/training/nightly tests passed: 157 passed,
1 skipped (optional training dependencies). This is iteration evidence;
integrated full-suite and exact-head hosted checks are recorded separately
in the work ledger. Earlier green checks do not qualify the new patch.

The final local Linux snapshot at baseline `ce0291bf` plus the integrated patch
passed **17,503 tests, 167 skipped**, three warnings and four subtests in
288.88 seconds. Six repository gates and the offline golden lanes (4/4 smoke,
33/33 policy) passed. A preceding rerun hit 12 adaptive-training free-space
guards on the bounded `/tmp` filesystem; the failed cohort and full suite
passed after disposable test storage was reclaimed, without relaxing guards.

Native Windows qualification includes the expanded focused cohort (533 passed,
12 explicit platform/profile skips), release smoke (one passed), MIC canaries
(20 passed, one Git Bash case deselected), the DATA qualification leaf (18
passed), telemetry overflow (two passed), and private inventory boundaries
(12 passed). The final native account-control run passed 43 tests; three
sidecar cases passed separately, including one overlap. Recovery HTTP passed
both tests in 91.06 seconds after the source checks were corrected. The full
Windows exploratory run was stopped at about eight percent after exposing
an intermittent HTTP failure; it is not counted as a full-suite pass.

Two account-source problems were addressed: equal immutable config snapshots
can survive provider replacement while changed values still revoke old
capabilities, and SQLite may legitimately remove its rollback journal during
a concurrent commit. A real transaction reproduced `FileNotFoundError` between
existence checking and sidecar metadata reads. Direct `lstat` now tolerates only
absence; unsafe existing links, non-files, reparse points and inspection errors
remain refused. The earlier intermittent 503 had no captured inner exception,
so the exact causal link to that incident remains unproven. The race itself is
reproduced and pinned by a failing-before/passing-after regression.
Exact-head hosted checks remain required before merge.
