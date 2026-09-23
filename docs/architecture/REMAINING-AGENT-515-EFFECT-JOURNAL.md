# Issue 515 — Effect intent/outcome journal and worker recovery

The typed tool gateway now has a live journal boundary for mutating calls. A
worker binds `JournalBinding` for its run, worker identity, owner epoch, and
scope. Immediately before the gateway invokes a call with a non-empty effect
set, `SQLiteEffectJournal` commits an intent containing the request digest,
idempotency key, and reconciliation strategy. A returned invocation publishes a
terminal outcome; an exception leaves the intent explicitly uncertain.

The SQLite journal is append-oriented. Repeating an identical intent returns
its original sequence to the journal caller, while the live tool gateway
refuses that replay before invoking the tool. Any change to the full admitted
identity conflicts. Terminal outcomes require the admitted worker and epoch,
cannot be replaced, and a receipt arriving after restart recovery has marked an
intent uncertain is refused as a late receipt. Recovery returns a bounded
decision: a confirmed live owner with the exact epoch may reattach its existing
intent; an unavailable owner is left for explicit reconciliation. No recovery
path executes a tool or manufactures success.

`SQLiteRuntimeCheckpointRepository` accepts the journal as an optional
dependency only when both use the same SQLite database. Saves record the
journal high-water mark and checkpoint generation in one SQLite transaction
and reject unresolved intents. Restore validates that binding and the
journal's current state before returning a checkpoint, so a checkpoint cannot
authorize replay across an unrecorded effect.

This slice extends the same guarantee to direct host-owned worker adapters.
After a terminal receipt, `journaled_effect` appends a checkpoint in the
worker-effects database. The host allocates the generation and captures the
an explicit trusted state projection as canonical JSON, generation, and journal
high-water in one transaction. The default projection is content-free metadata
only, so worker output and prompts are not copied into the journal.
Restore rejects a stale high-water or an unresolved intent, so a restart
cannot treat a partially published worker result as a safe replay point. The
same database durably records each `(run, worker, owner_epoch)` fence; restart
claims the newer epoch before recovery, and older bindings cannot admit new
effects afterward. A recovery-required fence also blocks every new operation
until the unresolved effect is explicitly reconciled. Advancing the owner
epoch does not clear that fence; this slice has no automatic reconciliation
clear path and therefore remains fail-closed. The journal accepts an immutable
operation-family verifier registry only during trusted bootstrap construction;
there is no post-construction registration method. Production bootstrap
currently supplies no verifier, so its fences cannot be positively cleared
until a real provider composition exists. A configured verifier runs outside
the journal write transaction under a bounded timeout and must return a typed
`ReconciliationProof` containing
the exact intent, operation, external receipt, outcome digest, verifier id,
and external reference. The journal applies that proof and clears the fence in
one epoch-checked transaction only when no unresolved effects remain. Unknown
operation families, stale epochs, malformed or conflicting proofs remain
fenced; there is no caller-controlled clear switch. Verifier admission is
bounded process-wide, and a timed-out provider retains its slot until its
thread actually exits. This bounds resource use while preserving fail-closed
behavior when a provider hangs.

The production bootstrap now composes one concrete verifier for the
`process-start:<job_id>` family. It reads the durable process job registry's
terminal record and derives a bounded outcome digest from the job identity,
kind, operation, idempotency key, status, and revision. The durable kind and
idempotency key must exactly match the journaled identity, and the durable
operation must be non-empty under the supported process contract.
The process adapter also persists a SHA-256 canonical request digest in the
existing bounded job metadata extension; the verifier requires that digest to
match the journal intent. Legacy rows without the digest remain fenced.
`succeeded` produces a completed proof; `failed` or
`cancelled` produces a failed proof. Pending, missing, malformed, or otherwise
unknown registry state produces no proof and leaves the fence set. The
verifier never uses process output, caller text, or an in-memory process handle
as authority. Other worker families remain unsupported and fenced.

Trust boundary: this is a cooperative host-process API, not an in-process
Python authentication boundary. Any code that can open the effects database
can construct a second journal with its own verifier registry, or mutate the
SQLite tables directly; the runtime cannot distinguish that code from trusted
composition. Supported extensions are launched by `ExtensionHost` in a child
process over bounded JSON-lines IPC and do not receive the journal object or
database path. Protection against a same-user process that independently
discovers and opens the database requires the host OS filesystem/process
boundary and is outside this module's claim.

The process adapter is exercised at its real worker boundary. A test starts a
real child process that performs one filesystem mutation, injects a crash after
launch and before the adapter returns a receipt, then reopens the journal.
The effect is `uncertain`, restart requires explicit reconciliation, and the
mutation remains exactly once in the fixture.

## Hard-crash cut coverage for every direct worker family

`tests/test_worker_effect_crash_injection.py` drives each direct mutating
worker family through its real adapter: `SubprocessJobProvider.start`
(`process-start`), `ComputeJobWorker.submit` (`compute-submit`),
`ComputeJobWorker.cancel` (`compute-cancel`), `LocalSubagentProvider.spawn`
(`subagent-run`), and `GuardedLegacySelfmodService.deploy`
(`selfmod-deploy`). Each case runs in a child interpreter against a
file-backed `SQLiteEffectJournal` and is killed with `os._exit` at one cut, so
no `except` or `finally` handler runs. The external effect appends to a marker
file, so duplicate execution is counted rather than inferred. The child's exit
status is asserted first; a case whose crash hook did not fire fails.

| Cut | What is on disk after the crash | Restart behavior |
|---|---|---|
| after intent, before the effect | intent, no receipt, no covering checkpoint; effect count 0 | restart refuses; the intent becomes `uncertain`; a late receipt from epoch 1 is refused |
| during the effect | as above; effect count 1 | same |
| after the effect, before the receipt | as above; effect count 1 | same |
| receipt applied inside the transaction, before checkpoint and `COMMIT` | SQLite rolls back the receipt: bare intent, no checkpoint | same; this shows the receipt and checkpoint are atomic |
| after the receipt+checkpoint `COMMIT` | `completed`; checkpoint high-water equals the intent sequence | restart resumes; restored checkpoint names the stored receipt |

For every family and cut, a restarted worker at a newer epoch retries the same
operation. The effect journal refuses it: the restart fence for unresolved
cuts, or intent-identity conflict or duplicate-intent refusal for the
committed cut. The marker count does not change. For `subagent-run`, the
refusal surfaces as a non-succeeded child, because the child runs on a worker
thread. That gives 25 hard-crash cases (5 families x 5 cuts).

A mutation check confirmed that the harness is not vacuous. Temporarily
committing the receipt before the checkpoint insert made all five
`in_receipt_txn` cases fail. The source was restored afterward. Removing only the
duplicate-intent refusal in `JournalBinding.begin_request` did not make the
`after_commit` cases fail, because the owner-epoch identity check in
`SQLiteEffectJournal.begin` also refuses the retry. The retry is therefore
defended at two layers. The single-layer duplicate refusal is covered by
existing same-epoch replay tests in `tests/test_effect_journal.py`.

### Defect fixed: post-invoke publication failure left a reattachable intent

Before this change, `journaled_effect` marked an intent `uncertain` only when
the worker invocation itself raised. Suppose the effect ran and then receipt
publication failed: a `success` predicate or `checkpoint_state` projection
raised, the state was not serializable, or the atomic outcome+checkpoint
transaction was refused. The intent stayed a bare `intent`. `recover()`
reattaches a bare intent to a live owner at the same epoch. This happens when
composition passes an authenticated liveness map. In that case, an effect that
had already run could be invoked a second time. `journaled_effect` now marks
every post-invoke publication failure `uncertain`. The detail records only the
exception type name. If the journal cannot record that, the original error still
propagates, and restart recovery treats the bare intent as orphaned. RED
evidence: `test_post_invoke_publication_failure_is_uncertain_not_reattachable`
and `test_success_predicate_failure_after_effect_is_uncertain` failed before
the fix (`state=intent`, `recover(...).action == "reattach"` path) and pass
after it.

Evidence:

- `sonder_runtime/adapters/persistence/sqlite/effect_journal.py`
- `sonder_runtime/application/execution/worker_bindings.py`
- `sonder_runtime/application/execution/effect_journal.py`
- `sonder_runtime/adapters/execution/process_jobs.py`
- `tests/test_effect_journal.py`
- `tests/test_worker_effect_bindings.py`
- `tests/test_worker_effect_crash_injection.py`

Focused verification:

- `python -m pytest -p no:cacheprovider -q tests/test_worker_effect_crash_injection.py`: 27 passed (25 hard-crash cases and 2 post-invoke regression tests).
- The 23 test files that import the effect journal, worker bindings, process jobs, compute jobs, subagent adapter, or self-mod service: 410 passed, 2 skipped (one needs a container runtime, one needs `/proc`).

Earlier verification of this slice:

- `python -m pytest -q tests/test_effect_journal.py tests/test_worker_effect_bindings.py tests/test_worker_capacity.py` — 50 passed, including verifier reconciliation, restart, stale epoch, replay, conflicting proof, and concurrent reconciler coverage.
- `python -m compileall -q sonder_runtime/application/execution/worker_bindings.py sonder_runtime/adapters/persistence/sqlite/effect_journal.py` — passed.
- `git diff --check` — passed.

Remaining limits:

- Hard-crash coverage now includes every direct worker family that calls
  `journaled_effect`. The typed tool gateway journal path
  (`gateway_contract.execute`) and interactive lanes are not part of this
  child-process crash matrix. Their existing tests cover exception-path
  uncertainty only.
- Crash cases use deterministic in-process effect doubles for the process
  launcher, compute provider, subagent runner, and legacy self-mod adapter. The
  earlier real child-process test covers only the process family's real OS
  launch.
- Legacy self-mod stages other than deploy and rollback still run outside the
  journal: `create_backup`, `prepare_workspace`, `record_reproducer_before`,
  `begin_testing`, `record_test`, `review`, and `approve`. This change is
  described here but not made. `_mutating_call` hardcodes a deploy-shaped
  success predicate and one `"{operation}:{run_id}"` operation ID per run. Each
  stage would need its own success predicate. Repeatable stages such as
  `record_test` also need a per-attempt operation identity, so that a
  legitimate retry is not refused as a duplicate. Without that identity, the
  journal would fence every retried test. This work belongs in
  `selfmod_service.py`, not in the #519 low-integrity nightly harness files.
- `compute-cancel` uses one operation ID per job. A second cancel of the same
  job is refused as a duplicate intent, even after a
  `cancellation_requested` receipt whose cleanup was pending. This was
  confirmed by a direct run. Retrying cancellation needs a per-attempt identity
  or a query-based reconciliation strategy.
- Compute, subagent, and self-mod operation families still have no provider
  verifier, so their fences can be cleared only by future trusted composition.
- A full hosted regression and deployment receipt are still required before
  LOOP-008 can be promoted to `verified`.
