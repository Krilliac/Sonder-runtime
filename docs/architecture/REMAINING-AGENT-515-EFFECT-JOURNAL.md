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

Evidence:

- `sonder_runtime/adapters/persistence/sqlite/effect_journal.py`
- `sonder_runtime/application/execution/worker_bindings.py`
- `sonder_runtime/application/execution/effect_journal.py`
- `sonder_runtime/adapters/execution/process_jobs.py`
- `tests/test_effect_journal.py`
- `tests/test_worker_effect_bindings.py`

Focused verification:

- `python -m pytest -q tests/test_effect_journal.py tests/test_worker_effect_bindings.py tests/test_worker_capacity.py` — 50 passed, including verifier reconciliation, restart, stale epoch, replay, conflicting proof, and concurrent reconciler coverage.
- `python -m compileall -q sonder_runtime/application/execution/worker_bindings.py sonder_runtime/adapters/persistence/sqlite/effect_journal.py` — passed.
- `git diff --check` — passed.

Remaining limits: this does not claim fault-injection coverage for every
worker family, and legacy self-mod preparation, backup, testing, review, and
approval remain outside the journal. A full hosted regression and deployment
receipt are also required before promoting LOOP-008 to `verified`.
