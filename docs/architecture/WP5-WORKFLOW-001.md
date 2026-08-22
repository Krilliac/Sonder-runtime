# WP5-WORKFLOW-001 — Restart-safe workflow recovery

Status: implementation slice complete; formal master-spec checkboxes remain unchanged.

`sonder_runtime/application/workflows/restart_recovery.py` defines the
persistence-neutral recovery contract for workflow executions. A
`WorkflowSnapshot` carries the execution identity, revision, next step,
state, owner, terminal outcome, and completed resume-key ledger. Store adapters
must persist updates with compare-and-set semantics.

The service provides four guarantees:

1. Interrupted executions and executions owned by another instance are
   explicitly classified as requiring restart.
2. A resume key is recorded with the resulting value. Repeating the same key
   returns the stored value without invoking the workflow callback again.
3. Compare-and-set rejects a concurrent snapshot change instead of allowing a
   stale worker to overwrite recovery state.
4. Terminal executions are observable as terminal, cannot be resumed, and
   terminal completion is idempotent.

Focused evidence: `tests/test_wp5_restart_recovery.py` covers restart
detection, idempotent resume, terminal handling, ownership, and concurrency
conflict behavior.
