# OPS-004 durable repair execution — 2026-08-21

## Implemented slice

`DurableRepairExecutor` connects the typed `ReconciliationResult` produced by
startup reconciliation to a local, journal-backed effect boundary. It accepts
only `RESUME`, `DELIVER`, and `MARK_INTERRUPTED` decisions with their matching
fail-closed classifications. Cleanup-process-tree, keep, skip, unknown, and
mismatched decisions are rejected before the effect callback runs.

The journal writes a durable `pending` record before invoking the callback and
changes it to `applied` only after the callback returns. A matching applied key
replays its recorded value without invoking the effect again. A matching
pending key raises `RepairRecoveryRequired`; it is never silently retried after
a crash. Reusing a key for a different decision raises `RepairConflict`.

## Boundary and limitation

The local seam provides durable decision/idempotency state and a typed callback;
it does not claim to execute OS process-tree cleanup, external outbox delivery,
or platform-specific job resume. Those effects require a separately deployed
supervisor/provider that can reconcile a pending record before continuing.

## Evidence

- `tests/test_ops004_durable_repair.py`: **3 passed** focused tests.
- `python -m compileall -q sonder_runtime tests`: passed.
- `python scripts/check_architecture.py`: passed.
- `python scripts/check_evidence_documents.py`: passed.
