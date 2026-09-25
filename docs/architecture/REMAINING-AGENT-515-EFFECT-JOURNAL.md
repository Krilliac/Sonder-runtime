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
*Superseded by the review fix below:* restore originally rejected any
checkpoint whose high-water differed from the journal maximum. Now a checkpoint
binds the run's settled prefix. Restore rejects a checkpoint when any effect is
unresolved or when the checkpoint is ahead of the journal. It reports
`journal_high_water` when later terminal effects exist, so a restart still
cannot treat a partially published worker result as a safe replay point. The
same database durably records each `(run, worker, owner_epoch)` fence; restart
claims the newer epoch before recovery, and older bindings cannot admit new
effects afterward. A recovery-required fence also blocks every new operation
until the unresolved effect is explicitly reconciled. Advancing the owner
epoch does not clear that fence; this slice has no automatic reconciliation
clear path and therefore remains fail-closed. The journal accepts an immutable
operation-family verifier registry only during trusted bootstrap construction;
there is no post-construction registration method. The original bootstrap supplied no verifier. Production composition now
supplies the bounded process-start and local compute verifiers described below;
unknown operation families remain fenced. A configured verifier runs outside
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
*Superseded by the review fix below:* before that fix, `succeeded` produced a
completed proof and `failed` or `cancelled` produced a failed proof, with
receipt `process-job:{id}:{rev}`. Now only a durable attach record (launch
state `attached` and a positive `process_id`) on a terminal job produces a
completed proof, with receipt `{job_id}:{pid}`. The journaled effect is the
start of the process. The job's exit status is not that effect's outcome.
Pending, missing, malformed, unattached, or otherwise unknown registry state
produces no proof and leaves the fence set. The
verifier never uses process output, caller text, or an in-memory process handle
as authority. Local compute-submit and its nested process-start also have the bounded
attachment proof described in the continuation section below. Other unknown
worker families remain unsupported and fenced.

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
(`subagent-dispatch`, originally `subagent-run`; see the dispatch section
below), and `GuardedLegacySelfmodService.deploy`
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
committed cut. The marker count does not change. For `subagent-dispatch` the
marker is the admitting `running` transition in the child store; the refusal
now surfaces from `spawn()` itself, because dispatch is journaled in the
spawning thread (the former `subagent-run` family surfaced it as a
non-succeeded child). That gives 25 hard-crash cases (5 families x 5 cuts).

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

## Review fixes (PR #523 review of 3d22e670)

An independent review reproduced five defects. Each fix has a regression test
in `tests/test_effect_journal_review_523.py`. All 18 tests in that file failed
against the pre-fix sources (the four source files checked out from
`3d22e670`), and all 18 pass after the fix. The reviewer's repro script now
prints success for R1 through R3. R4 is now a `TypeError`: `append_checkpoint`
requires an owner identity, and the stale epoch is refused.

- **P1-1: concurrent effects in one run.** Production process and compute
  workers share one run ID. Previously, when A was in flight while B was
  admitted, A's `outcome_and_checkpoint` refused the checkpoint and rolled back
  A's outcome. A was then marked `uncertain` even though it had launched. Now
  the terminal outcome always commits, and the checkpoint binds the settled
  high-water: the largest fully terminal prefix, which never moves backwards.
  Tests cover interleaved calls and 8 real threads held open at a barrier, and
  confirm restart succeeds afterward.
- **P1-2: restart wedged after reconciliation.** Previously, `restore_checkpoint`
  required the checkpoint high-water to equal the journal maximum, so a
  verified `reconcile()` left restart failing forever with a "stale
  high-water" error. Restore now accepts later records when every effect in
  the run is terminal. It returns `journal_high_water`, and the caller reads
  the later records with `effects_since`. Restore still refuses unresolved
  effects and refuses a checkpoint ahead of the journal. The test runs crash,
  reconcile, `recover_before_restart`, and gets `resume`. The test previously
  named `test_worker_checkpoint_rejects_effect_admitted_after_last_checkpoint`
  was rewritten to this semantics, and it still asserts refusal once a later
  intent is unresolved.
- **P2-1: fence cleared only for the reconciled worker.** `recover()` fences
  every owner in the run. `reconcile()` now clears the fence for the whole run
  once nothing in the run is unresolved, instead of only for the reconciled
  worker.
- **P2-2: checkpoint writes had no owner fence.** `append_checkpoint` now takes
  keyword-only `worker_id` and `owner_epoch`, and requires the current,
  unfenced owner. `outcome_and_checkpoint` makes the same check inside its
  transaction; a stale owner is refused and its outcome is rolled back. An
  idempotent replay of an already-recorded outcome appends no generation and
  returns `None`.
- **P3.**
  - The protocol's `append_checkpoint` signature now matches the
    implementation.
  - Checkpoint generations are pruned to the latest `CHECKPOINT_RETENTION`
    (16) per run.
  - The process verifier now reports a launched start as `completed` with the
    live-path receipt shape `{job_id}:{pid}`. A test covers a real
    `DurableJobRegistry` attach record.

Correction to earlier claims: the 25 hard-crash cuts above each run one
effect at a time. They did not exercise overlapping effects in one run, which
is how P1-1 went unnoticed. Revision 12 did not demonstrate a successful
restart after verification; P1-2 shows it could not. LOOP-008 revision 15
records these corrections.

Verification after the fixes: the 27 test files that import the journal,
bindings, process jobs, compute jobs, subagents, selfmod service, or runtime
checkpoints: 477 passed, 2 skipped. One skip needs a container runtime and one
needs `/proc`.

## Read API for checkpoint binding (for the #510 worker-registry saga)

PR #541 designed a saga to close this gap: `worker-effects.db` can be ahead
of the last `child-sessions.db` checkpoint after a crash. That saga needs a
read-only journal position. `SQLiteEffectJournal` now implements
`EffectJournalReader`, which is defined in
`sonder_runtime/application/execution/effect_journal.py`:

- `settled_high_water(run_id: str) -> int` returns the largest sequence `S`
  such that every intent of the run with `sequence <= S` is `completed` or
  `failed`. It returns 0 when the run has no intents or its first intent is
  unresolved. An `intent` or `uncertain` row caps the value just below itself,
  and resolving that row advances the value. Sequences are allocated
  contiguously per run, so this is `MIN(unresolved sequence) - 1`, or
  `MAX(sequence)` when nothing is unresolved.
- `effects_since(run_id: str, after_sequence: int, *, limit: int = 100,
  worker_id: str | None = None) -> EffectJournalPage` returns a bounded list
  of intents with `sequence > after_sequence`, in sequence order. The page has
  these fields:
  - `records`: full `EffectIntent` values, including state, idempotency key,
    receipt key, and owner epoch.
  - `high_water`: the maximum sequence for the whole run.
  - `settled_high_water`: the settled sequence for the whole run. Both
    high-water values ignore the `worker_id` filter.
  - `truncated`: true when more records exist. The caller then pages with
    `after_sequence=records[-1].sequence`.
  - `unresolved`: a property listing the records in `intent` or `uncertain`
    state.

  All values come from one deferred SQLite read snapshot. `limit` must be in
  `1..10000`, `after_sequence` must be an `int >= 0`, and invalid input raises
  `EffectJournalError`.

Neither method writes anything. They do not claim ownership or touch
`recovery_required`. A test compares every journal, owner, and checkpoint row
before and after reads made while a fence is set. The intended consumer flow
from the #541 design is:

1. Before each checkpoint CAS, store `settled_high_water(run_id)` as
   `effect_high_water`.
2. On resume, call `effects_since(run_id, effect_high_water)`. Settled records
   map idempotency keys to stored receipts, and they are not re-invoked. Any
   entry in `unresolved` means restart is refused through
   `recover_before_restart`.

Tests: `tests/test_effect_journal_reader.py`, 14 passed. A mutation check made
`settled_high_water` ignore unresolved rows, and 4 of those tests failed. The
source was then restored. This PR does not change any #541 files.

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
- Compute-cancel and self-mod operation families still have no provider
  verifier, so their fences can be cleared only by future trusted composition.
  Compute-submit and subagent-dispatch verifiers are described below; they
  prove launch or admission only, never workload or runner success.
- A full hosted regression and deployment receipt are still required before
  LOOP-008 can be promoted to `verified`.


## Continuation qualification on 2026-09-24

Supported native and legacy typed file mutations now declare canonical
`write_files`/`delete_files` effects. Trusted edge permissions carry those
host-owned descriptors into ToolGateway; caller restrictions and ordinary
resource policy remain enforced. Real writes create completed journal entries;
reads and denied calls create none. A subprocess crash immediately after an
actual typed append but before receipt leaves one uncertain intent after
restart and refuses a second append (`test_typed_file_effect_crash.py`).

`test_tool_gateway_effect_crash_matrix.py` separately covers gateway crash cuts,
overlapping effects, settled-prefix checkpoint recovery, and failures during
redaction/receipt publication after the physical effect. Failed uncertainty
publication preserves the original error and does not certify completion.

Production composition adds local compute-submit and nested process-start
verifiers bound to the durable process registry. Exact job/worker/controller,
idempotency, scope, attached PID and both request digests are required. A terminal
failed/cancelled workload can prove that its launch occurred; it cannot prove
successful workload execution. Missing, legacy, running, unattached, or mismatched
records produce no proof. Real child-process crash tests cover both receipt
boundaries and assert stale-owner refusal and no second launch.

Issue #515 remains open. Child-session checkpoint generations still lack a
journal-high-water saga; whole-child fencing is not proof of safe continuation
from every child checkpoint. Compute cancel, subagent and self-mod status text
cannot supply independent effect receipts, and legacy self-mod intermediate
stages are outside the currently qualified deploy/rollback boundary. These gaps
must remain explicit rather than being hidden by the passing direct-worker
matrix.

### Concrete child-checkpoint blocker and next implementation

*Superseded by the bounded dispatch effect below:* `LocalSubagentProvider`
originally wrapped the entire runner in one `subagent-run:{child_id}` effect.
That first intent stayed unresolved while the runner saved child checkpoints
and completed inner tool effects in the same run, so even a journal containing
a completed inner mutation had a settled high-water of zero until the outer
runner returned.

#### Bounded `subagent-dispatch` effect (item 1 below)

`LocalSubagentProvider` now journals one `subagent-dispatch:{child_id}` effect
around admission only (`DurableContinuationService.spawn`), in the spawning
thread, with reconciliation strategy `query`. The binding factory runs before
the intent, so a fenced run is refused before any admission. The runner thread
is gated: it waits (bounded by 30 s and the operation deadline) until the
dispatch receipt and checkpoint commit, and fails closed if the receipt is not
published. It then runs under the same binding, so inner effects are journaled
in the same run above the settled dispatch.

The receipt proves exact durable admission, not runner completion. The
provider and the new host verifier
(`adapters/execution/subagent_dispatch_verifier.py`) derive it from the child
store only. They recompute a canonical request digest from the persisted request
(child, parent, prompt, budget, metadata, resume and idempotency keys). They
require an exact child, parent, idempotency key, and digest match. They take the
admitted revision from the retained receipt of the first applied `running`
update in the store's mutation log. The receipt key is
`subagent-dispatch:{child_id}:{admitted_revision}`. The outcome digest binds
those fields, and the live and reconciled values are identical. The child
store schema is unchanged. A missing row, a digest, parent, or idempotency
mismatch, a wrong run, scope, or strategy, and a legacy or unstarted row
without a retained admission record all produce no proof. Terminal status text
alone is not used. A synchronous admission refusal with no durable admission
is recorded as a `failed` dispatch, not a fence. Reuse of a settled dispatch
returns the existing child only while the child store still proves that
admission, and it cannot start a runner. A repeat spawn of a child whose runner
this provider launched and which is still live does not compose a new binding
(composition runs restart recovery, which would fence the live runner's
in-flight inner effects); it only returns the live handle or is refused, and
dispatch is serialized per provider. Production composition registers the
verifier in `get_worker_effect_journal` through a lazy continuation-repository
getter, and passes the same kind of verifier to the provider.

Evidence (focused, not a requirement verification):
`tests/test_subagent_dispatch_effect.py` covers the dispatch being completed
before the runner's first inner effect and a settled high-water of 2 while the
child is running. It also covers registry-reserved delegation, reuse, refusal,
and verifier no-proof cases. A real `os._exit` after admission but before the
receipt leaves an intent. A second spawn is refused. A verifier bound to a
store without the row gives no proof. The exact row reconciles it, and the
settled dispatch still starts no runner. `test_child_effect_checkpoint_crash.py`
was rewritten deliberately to these semantics. After a real crash mid-run,
the dispatch and inner receipts are `completed` and the settled high-water is
2. Restart still requires owner cleanup (`ContinuationCleanupRequired`), a
restarted provider cannot launch a second runner, and the inner write is
refused rather than re-invoked. `tests/test_worker_effect_crash_injection.py`
now runs the `subagent-dispatch` family through all five cuts.
`tests/test_515_bounded_subagent_dispatch_effect_bootstrap.py` checks the
composition wiring.

The settled prefix now exists, but nothing consumes it yet. The remaining
items below are unchanged, and restart of a child whose runner crashed after
dispatch remains non-resuming.

The child checkpoint CAS in `application/subagents/durable_continuation.py`
stores no journal provenance. Existing restart paths correctly require owner
cleanup and refuse a second runner. They prevent duplicate execution
but cannot continue from the saved child state. The next implementation needs:

1. Implemented as described above: a bounded dispatch effect whose receipt
   proves exact durable child admission, with an identity and request digest,
   rather than completion of the entire runner. Crashes across dispatch still
   refuse an unproven second start.
2. Host-stamped checkpoint provenance binding child sequence/state digest,
   journal identity, run, worker, owner epoch, and settled position to the child
   CAS. SQLite child storage, PostgreSQL snapshots, and the continuation codec
   all need compatible handling; legacy rows without provenance must refuse.
3. A cross-store commit protocol that records journal proof before child CAS.
   Resume must clean up the old owner, claim/reconcile the journal epoch, and
   validate bounded `effects_since` pages against the checkpoint. The runner
   must consume already-settled receipts or block; merely returning them is
   insufficient to prevent replay. Production currently composes the effect
   binding around spawn, and exposes no bound child resume adapter. A future
   resume entry point must validate that binding before claiming the child;
   calling the bare continuation service cannot establish journal authority.
4. Real crash cuts around dispatch, inner mutation, both checkpoint stores, and
   terminal publication, including stale epochs, missing or swapped journals,
   truncated receipt pages, and overlapping unresolved intents.

This is a coordinated lifecycle and persistence change, not an extra checkpoint
field. Terminal child status text is not a substitute for a dispatch receipt.

## Child checkpoint journal provenance on 2026-09-25 (item 2, validation half of item 3)

This slice implements item 2 of the list above and the validation half of
item 3, for the SQLite child store and the continuation codec. It does not
change `LocalSubagentProvider`, the outer `subagent-run` effect, or add a
production resume adapter. Item 1 (the dispatch receipt) belongs to a
separate slice.

What now exists:

- `ContinuableCheckpoint.provenance` holds an optional immutable
  `CheckpointProvenance`, defined in `application/subagents/continuable.py`.
  It binds `(child_id, sequence, canonical state digest, cursor)` to
  `(journal identity, run_id, worker_id, owner_epoch, settled position)`. A
  `record_digest` covers every field. `None` means provenance-absent.
- `DurableContinuationService(repository, checkpoint_provenance=hook)`
  accepts an injectable host hook. The runner's `save(state, cursor)` callable
  has no provenance parameter. The service computes the state digest, calls
  the hook, and refuses a record whose subject or digest does not match. A
  refusal fails the save and the child keeps its previous checkpoint. A
  `provenance` key inside the runner's state is plain data. Without a hook,
  behaviour is unchanged and checkpoints are stored provenance-absent.
- `JournalProvenanceStamp` (`application/subagents/checkpoint_provenance.py`)
  is the host hook. It reads one journal snapshot: identity, current owner
  epoch and settled high-water. It refuses to stamp for an owner that is not
  the current epoch, or when the journal identity is missing. It records the
  settled prefix, not the high-water, so an open outer intent pins the
  position below itself.
- `SQLiteDurableContinuationRepository` stores provenance in an additive
  `child_checkpoint_provenance` table, keyed by `(child_id, sequence)`. The
  row is inserted in the same `BEGIN IMMEDIATE` transaction as the checkpoint
  compare-and-set and the mutation receipt. Triggers refuse `UPDATE` and
  `DELETE`. Every read left-joins the row for the current checkpoint, so
  rows written before the table existed read back provenance-absent. The
  store also refuses provenance whose subject does not match the checkpoint.
- `SQLiteJournalProvenanceSource` gives a SQLite effect journal a durable
  random identity. The identity lives in an additive
  `effect_journal_identity` table in the journal file and is minted only when
  trusted composition passes `create_identity=True`. Validation reads open the
  file read-only and never mint an identity, so a missing or recreated
  journal refuses.
- `continuation_codec` round-trips provenance, and snapshots without the
  field decode as absent. `postgres_continuation` exposes
  `encode_child_snapshot` and `decode_child_snapshot`, and its
  `save_checkpoint` applies the same subject check. In PostgreSQL, provenance
  lives inside the single child snapshot, so it commits with the
  compare-and-set by construction.
- `validate_checkpoint_resume` is a pure decision. It reads the whole run
  through bounded, contiguous `effects_since` pages. It returns
  `allowed=True` with `receipts` (settled outcomes at or below the position)
  and `later_receipts` (settled after it), each keyed by idempotency key.
  Otherwise it returns a typed `CheckpointResumeRefusal`: no checkpoint,
  provenance absent, record digest, subject or state digest mismatch, journal
  missing, unavailable or swapped, run or worker mismatch, stale resumer
  epoch, epoch ahead, superseded owner, position ahead of the journal, an
  unresolved intent at or below the position, an unresolved intent reusing a
  settled idempotency key, any other unresolved intent, an incomplete or
  inconsistent page, or an exhausted page budget. After the last page it
  re-reads the journal identity and current owner epoch and refuses with
  `journal_changed_during_validation` if either moved while pages were read.

Tests: `tests/test_child_checkpoint_journal_provenance.py`. They include
real `os._exit` crash cuts in a child interpreter at three points: after the
journal receipt commits but before the child compare-and-set, inside the
compare-and-set transaction before `COMMIT`, and after the compare-and-set.
Reopening both files yields either the old checkpoint with its old valid
provenance or the new one. No reopened checkpoint names a position above the
journal's settled high-water, and the validator accepts each one. The receipt
after the old checkpoint appears in `later_receipts`.

Verification on 2026-09-25:

- The new file has 16 tests, all passing (17 after review added the
  swapped-or-reclaimed-during-paging case). Before the implementation existed,
  the file failed at collection.
- Mutation check: 11 planted defects each made at least one test fail, and
  the sources were then restored. The defects disabled the state-digest,
  page-end, contiguity, stale-epoch, journal-identity, unresolved-below and
  overlap checks; stamped the high-water instead of the settled prefix;
  dropped the service hook; dropped the SQLite provenance insert; and dropped
  the PostgreSQL subject check.
- The 19 existing continuation, child-storage, child-migration, worker
  registry, subagent provider and effect-journal test files, together with the
  new file: 205 passed, 23 skipped. The skips need a PostgreSQL binding.
- The 19 other test files that import the child store or provider: 222
  passed, 13 skipped (PostgreSQL or Windows only).
- The first run found that `child_migration.py` splits the child DDL on `;`.
  The immutability triggers are therefore installed separately, when the
  repository opens a database.

What is not qualified:

- There is no production resume path. Nothing in `LocalSubagentProvider` or
  bootstrap installs the hook or calls the validator. The outer
  `subagent-run:{child_id}` intent still pins the settled position at 0 for
  every production child checkpoint, so production checkpoints would be
  refused. Item 1 (the dispatch receipt) and a bound resume entry point are
  required before any checkpoint can authorize continuation. Whole-child
  fencing remains the only production guarantee.
- The validator returns receipts. It does not make a runner consume them.
  Runner-side consumption, and crash cuts around dispatch and terminal
  publication, remain open (items 3 and 4).
- The journal and child store are separate files. The protocol is
  journal-proof-first, with the validator covering the gap. It is not a
  cross-store transaction. The owner-epoch check in the stamp and the child
  compare-and-set are not one atomic step. A newer owner that claims between
  them is detected at validation (`owner_superseded` or `stale_owner_epoch`),
  not prevented at write time.
- PostgreSQL is qualified at the codec and `_apply` level only, with no live
  database. The SQLite-to-PostgreSQL child migration copies
  `durable_child_session` rows only, so migrated checkpoints arrive
  provenance-absent (fail-closed). A child migration that was paused before
  this change and resumed after it recomputes page digests over snapshots
  that now carry `"provenance": null`, so the recorded page digests no
  longer match and the resume is refused (fail-closed; restart the
  migration).
- A `save_checkpoint` intent that was retained unresolved before this change
  cannot be replayed through `mutate`, because the payload now includes the
  `provenance` field. It stays fenced as an ambiguous mutation, and receipt
  reconciliation is unchanged.
- No master-spec checkbox changes. LOOP-008, AGENT-006 and SESSION-007 stay
  unverified.

## Production wiring: startup reconciliation, stamped checkpoints and child resume (2026-09-25)

This slice connects the mechanisms above to real production callers. Every
item below is reached from `build_application` in `sonder_runtime/bootstrap/app.py`,
which the runtime entry points (`python -m sonder_runtime` and the HTTP
server, through `default_app`) compose. The
end-to-end tests drive that composition, not hand-built services.

*Superseded by this section:* the "What is not qualified" list under the
provenance slice says that nothing installs the hook or calls the validator,
and that there is no production resume path. Both statements are now false
for the SQLite child store.

What is now wired (caller -> callee):

- **Automatic bounded reconciliation.**
  - At startup, `build_application` calls `reconcile_worker_effects()`, which
    calls `application/execution/effect_reconciliation.reconcile_unresolved_effects`.
    That pass pages `SQLiteEffectJournal.unresolved_page` (a new read-only
    keyset query). For each run whose unresolved intents were all admitted by
    a worker identity this host owns (`process`, `compute`, `subagent`,
    `selfmod` on this node), it calls
    `AuthenticatedWorkerBinding.reconcile_before_restart`. That method claims
    the host epoch, calls `recover()` so orphans become `uncertain` and the run
    is fenced, then calls `journal.reconcile()` for each of this worker's
    intents through the immutable verifier registry.
  - Before any restart, the production `worker_binding()` sets
    `auto_reconcile=True`. `recover_before_restart` therefore runs the same
    bounded reconciliation before it refuses. This covers the process
    provider and compute worker constructors, `_compose_subagent_binding`
    (spawn and child resume) and `_compose_selfmod_binding`.
  - Bounds: at most 64 runs and 16 pages of 100 intents per startup pass, a
    20 s wall budget, the journal's recovery page per run, and a 2 s
    timeout on each verifier call.
  - Observability: a log line for each run and for the whole pass. The
    content-free `worker.effects.reconciled` event goes to the durable
    operations sink. Each proof is recorded in the journal's durable
    `verified:<verifier>:<reference>` detail.
  - Crash safety: every step is read-only or a single journal transaction. A
    second pass skips terminal intents.
  - Nothing invokes an effect. An intent with no proof stays `uncertain` and
    its run stays fenced.
  - A run that contains another host's worker identity is left untouched and
    reported under `foreign_runs`.
  - Live local peers (review fix). Worker identities are per node, so a
    second runtime process on the same node (for example `serve` plus an
    IDE-launched `mcp`) composes the same `<family>:<node>` identities. Each
    process that composes the worker-effects journal holds an exclusive OS
    file lock on its own lease under `worker-effect-hosts/` beside the
    journal (`adapters/persistence/worker_effect_hosts.py`), acquired before
    any intent can be admitted and held until the process exits. The startup
    pass first probes the other leases. If one is held, or the probe fails, it
    claims nothing and reports `deferred="live-peer-host-process"` (also in
    the event); a peer's in-flight intents and owner epoch stay untouched.
    Leases of exited processes are reaped when their lock is acquired. A
    pass that finds nothing unresolved claims nothing and emits no event.
    Before this fix the pass fenced a live peer's in-flight effects and made
    its receipt commit fail.
  - A failed pass is logged and keeps every fence in place. It does not stop
    composition.
  - Operators can run the pass again through
    `Application.worker_effect_reconciliation`.
  - Bindings that tests construct directly keep the old explicit-reconcile
    semantics (`auto_reconcile=False`).
- **Stamped child checkpoints.** `get_delegation_service` composes
  `SQLiteJournalProvenanceSource(get_worker_effect_journal(), create_identity=True)`
  and `DurableContinuationService(..., checkpoint_provenance=JournalProvenanceStamp(...))`.
  The binding resolver maps `ProvenanceSubject.child_id` to the same run,
  worker and epoch as `_compose_subagent_binding`. Every production child
  checkpoint save therefore runs `_stamp_checkpoint` and
  `CheckpointProvenance.stamp`. The SQLite store persists the record in the
  same transaction as the checkpoint compare-and-set.
- **Child resume path.** `LocalSubagentProvider` receives the same
  provenance source as `provenance_journal`. `LocalSubagentProvider.resume`
  follows these steps, and an exact repeat of a crashed or recoverable
  child's request through `spawn` / `DelegationService.dispatch` takes the
  same route:
  1. Prove the old owner dead with `DurableContinuationService.release_dead_owner`.
     This works only for registry reservations whose recorded pid and host are
     another, provably dead process. Anything else raises
     `ContinuationCleanupRequired`.
  2. Compose the binding. This claims a newer epoch and reconciles.
  3. Require a settled dispatch receipt that the child store still proves.
  4. Run `validate_checkpoint_resume`, or run `validate_uncheckpointed_resume`
     for a child that never checkpointed. The second is allowed only when
     the run holds nothing but settled dispatch attempts.
  5. Claim the exact validated revision with `resume(expected_revision=...)`.

  The resumed runner runs with `resumed_from(decision)` and
  `settled_receipts(...)` bound. `resume_receipts()` and
  `effect_journal.settled_receipt(key)` hand the runner the receipts settled
  at or before the checkpoint and those settled after it.
  `JournalBinding.begin_request` refuses one of those keys with
  `SettledEffectReplay` before any journal write. A refused validation
  raises `ChildResumeRefused` with the typed `CheckpointResumeRefusal`
  reason, and the child stays `FAILED`/`recovery_required`.
  `ContinuableCheckpoint.provenance_absent` is now the check that the
  validator uses.
- **Concurrent spawn decision (operator).**
  - Two identical concurrent dispatches join. `ContinuationWorkerRegistry`
    returns the reservation the other caller created when the launch is
    identical, and the provider's live-runner join returns the same handle.
    One runner and one `completed` dispatch intent result.
  - The same child identity with a different request digest is refused with
    `InvalidSubagentRequest` before any journal write.
  - A dispatch refused synchronously, with no durable admission, is recorded
    as a `failed` no-effect attempt (receipt `subagent-dispatch-refused:...`).
    A corrected dispatch is journaled as the next bounded attempt: operation
    `subagent-dispatch:{child}#dispatch-attempt-N` and idempotency key
    `{key}#dispatch-attempt-N`, with N up to 8. The dispatch verifier parses
    and proves attempts.
  - An unresolved or uncertain prior attempt still refuses (fail-closed).

Evidence (end-to-end through `build_application`):

- `tests/test_wiring_journal_child_startup_reconcile.py`: a real child
  interpreter is killed with `os._exit` after durable child admission and
  before the dispatch receipt. The next composition:
  - proves the dispatch from the child store;
  - leaves an unprovable selfmod intent `uncertain` with its run fenced;
  - leaves a foreign worker's intent untouched;
  - emits the operations event;
  - resolves nothing new on a second pass;
  - then resumes the admitted child through the exact delegation, with no
    second dispatch intent.
- `tests/test_wiring_journal_child_resume.py`: a real child interpreter
  dispatches through `DelegationService`. The runner checkpoints, performs a
  journaled append, and is killed with `os._exit` after the receipt and before
  its next checkpoint. The repeated delegation in a new composition finds a
  stamped checkpoint, resumes from it, and consumes the settled append
  receipt: the file holds one append and the run holds one append intent. A
  swapped journal identity refuses with `JOURNAL_IDENTITY_MISMATCH` and leaves
  the child `recovery_required`.
- `tests/test_wiring_journal_live_peer_startup.py`: a real child
  interpreter composes the application and admits an intent it keeps in
  flight. A second `build_application` leaves the intent `intent` and the
  owner epoch unchanged and reports the pass deferred. The peer then commits
  its receipt, and once it has exited the next pass is no longer deferred.
- `tests/test_wiring_journal_child_spawn.py` covers three cases: concurrent
  identical dispatch joins with one runner and one intent; a different digest
  is refused; a refused dispatch is followed by a corrected dispatch as
  attempt 2.

What remains:

- PostgreSQL child storage has provenance stamping at the codec level, but
  the resume path has been exercised only with the SQLite child store.
  `release_dead_owner` relies on the reservation's recorded pid/host, so a
  child started without a worker-registry reservation still needs manual
  owner cleanup (`ContinuationCleanupRequired`).
- Runner-side consumption is cooperative for effects outside the journal.
  Journaled effects are refused (`SettledEffectReplay`) if re-admitted, but
  the production conversational runner makes model calls, not journaled tool
  effects. Its model-attempt ledger is the session store, not the effect
  journal.
- The typed tool gateway uses a fresh `request_id` per call as its
  idempotency key. A resumed runner that re-issues a gateway tool call
  therefore gets a new key: the journal still records that call, but it is
  not matched to the settled receipt. Deterministic gateway request ids for
  child runners are not implemented.
- Compute-cancel and selfmod families still have no provider verifier, so
  startup reconciliation leaves them fenced. That is correct and fail-closed,
  but clearing them still needs future trusted composition.
- The journal and child store remain separate files. The cross-store window
  is covered by validation, not by a transaction.
- While any peer runtime process on the node is live, the startup pass is
  deferred as a whole, so a crashed third process's orphans stay fenced until
  their worker is recomposed (pre-restart path) or a later startup finds no
  live peer. The lease is consulted only by the startup pass: a peer
  process that lazily composes the process or compute provider still claims
  the shared `runtime:process-jobs` / `runtime:compute-jobs` run in its
  constructor (behaviour that predates this slice). Peers in one process
  share one lease; the lease is local OS
  evidence and does not coordinate hosts sharing a journal over a network
  filesystem.
- No master-spec checkbox changes. LOOP-008, AGENT-006 and SESSION-007 stay
  unverified.
