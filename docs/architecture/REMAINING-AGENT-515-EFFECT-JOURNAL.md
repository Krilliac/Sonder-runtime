# Issue 515 — Effect intent/outcome journal and worker recovery

The typed tool gateway now has a live journal boundary for mutating calls. A
worker binds `JournalBinding` for its run, worker identity, owner epoch, and
scope. Immediately before the gateway invokes a call with a non-empty effect
set, `SQLiteEffectJournal` commits an intent containing the request digest,
idempotency key, and reconciliation strategy. A returned invocation publishes a
terminal outcome; an exception leaves the intent explicitly uncertain.

The SQLite journal is append-oriented and idempotent. Repeating an identical
intent returns the original sequence, while a changed request digest conflicts.
Terminal outcomes cannot be replaced, and a receipt arriving after restart
recovery has marked an intent uncertain is refused as a late receipt. Recovery
returns a bounded decision: a confirmed live owner may reattach its existing
intent; an unavailable owner is left for explicit reconciliation. No recovery
path executes a tool or manufactures success.

`SQLiteRuntimeCheckpointRepository` accepts the journal as an optional
dependency. Saves record the journal high-water mark in the same SQLite
database and reject unresolved intents. Restore validates that binding and the
journal's current state before returning a checkpoint, so a checkpoint cannot
authorize replay across an unrecorded effect.

Evidence:

```text
python -m pytest -q tests/test_effect_journal.py tests/test_runtime_checkpoints.py
14 passed
python -m pytest -q tests/test_crosscutting_tool_gateway.py tests/test_seam002_typed_gateway.py
8 passed
```

The production composition still has to bind a concrete journal for each
long-lived worker run. The journal and gateway seams are live and tested; an
unbound gateway deliberately retains the existing behavior for foreground
callers.
