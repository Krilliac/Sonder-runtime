# REMAINING-LOOP-007-008 — Durable retry and idempotency state

## Scope

This slice closes the persistence gap left by the existing loop retry policy.
The policy remains the authority for bounded retry classification and
reconciliation requirements; the adapter supplies durable storage for
idempotency aggregates, outbox events, and retained retry evidence.

## Contract evidence

- `SQLiteLoopStateRepository` implements the existing `OutboxCASRepository`
  port. Idempotency records and their matching outbox events are committed in
  one SQLite transaction with monotonic revisions.
- `OutboxIdempotencyStore` therefore survives process/store recreation, rejects
  fingerprint reuse, and keeps unknown outcomes behind explicit reconciliation.
- `SQLiteRetryEvidenceLedger` retains bounded `RetryEvidence` rows across
  process recreation, including the policy's reconciliation-required action and
  classification.
- This adapter does not sleep or execute retries; transport adapters still own
  jitter/backoff and side-effect reconciliation.

## Verification

- `tests/test_remaining_loop_007_008.py`
- `python -m pytest -q tests/test_remaining_loop_007_008.py`
- `python scripts/check_architecture.py`
- `python -m compileall -q sonder_runtime`

The master checklist and requirement audit are intentionally unchanged.
