# REMAINING-LOOP-006-008 — Durable loop control

## Scope

This slice closes the application integration gap for LOOP-006 cancellation
propagation, LOOP-007 bounded retry evidence, and LOOP-008 persistent
idempotency/reconciliation. It composes existing domain policies and ports; it
does not perform provider, process, queue, or clock-sleep I/O.

## Contract evidence

- `DurableLoopControl.bind` associates every cancellable stream, tool,
  subprocess, terminal, subagent, job, training, update, or self-mod verifier
  with a cancellation node and lifecycle callbacks.
- `cancel_and_cleanup` propagates the request through the existing tree and
  returns conformance evidence only when cleanup reports both quiescence and
  resource release. A provider cannot be reported clean merely because cancel
  was requested.
- `DurableLoopControl.retry` delegates classification and side-effect safety to
  `domain.loop_retry_policy`, retaining a bounded immutable `RetryEvidence`
  ledger. Unknown outcomes and non-idempotent effects require reconciliation;
  no blind replay is authorized by this layer.
- `OutboxIdempotencyStore` persists immutable versioned idempotency aggregates
  and matching outbox events through the existing CAS repository. Reusing a key
  with a different fingerprint is rejected; unknown outcomes transition only
  through explicit reconciliation.

## Verification

`tests/test_remaining_loop_control.py` covers root-to-child cancellation,
cleanup conformance, bounded retry evidence, non-idempotent unknown-outcome
protection, durable outbox records, fingerprint conflicts, and reconciliation.

Formal master-spec checkboxes are intentionally unchanged.
