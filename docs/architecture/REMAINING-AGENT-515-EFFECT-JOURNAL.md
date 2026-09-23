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
state as canonical JSON, generation, and journal high-water in one transaction.
Restore rejects a stale high-water or an unresolved intent, so a restart
cannot treat a partially published worker result as a safe replay point. The
same database durably records each `(run, worker, owner_epoch)` fence; restart
claims the newer epoch before recovery, and older bindings cannot admit new
effects afterward. A recovery-required fence also blocks every new operation
until the unresolved effect is explicitly reconciled.

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

- `python -m pytest -q tests/test_effect_journal.py tests/test_worker_effect_bindings.py` — 23 passed.
- `python -m compileall -q sonder_runtime/application/execution/worker_bindings.py sonder_runtime/adapters/persistence/sqlite/effect_journal.py` — passed.
- `git diff --check` — passed.

Remaining limits: this does not claim fault-injection coverage for every
worker family, and legacy self-mod preparation, backup, testing, review, and
approval remain outside the journal. A full hosted regression and deployment
receipt are also required before promoting LOOP-008 to `verified`.
