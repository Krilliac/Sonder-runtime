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
cannot be replaced, and a receipt arriving after restart
recovery has marked an intent uncertain is refused as a late receipt. Recovery
returns a bounded decision: a confirmed live owner with the exact epoch may reattach its existing
intent; an unavailable owner is left for explicit reconciliation. No recovery
path executes a tool or manufactures success.

`SQLiteRuntimeCheckpointRepository` accepts the journal as an optional
dependency only when both use the same SQLite database. Saves record the
journal high-water mark and checkpoint generation in one SQLite transaction
and reject unresolved intents. Restore validates that binding and the
journal's current state before returning a checkpoint, so a checkpoint cannot
authorize replay across an unrecorded effect.

Evidence:

```text
python -m pytest -q tests/test_effect_journal.py tests/test_runtime_checkpoints.py
17 passed
python -m pytest -q tests/test_crosscutting_tool_gateway.py tests/test_seam002_typed_gateway.py
8 passed
```

Production agent lanes bind a persistent journal around the real tool gateway.
Focused bindings now cover process launch, compute submission and cancellation,
local subagent execution, and self-mod deployment and rollback when trusted
composition supplies an `AuthenticatedWorkerBinding`. The binding contract is
exercised by `tests/test_worker_effect_bindings.py`. Root composition still
needs to provide those bindings to every worker family. Legacy self-mod
backup, preparation, tests, review, and approval remain outside the journal.
Foreground callers without a worker binding retain their existing behavior.
