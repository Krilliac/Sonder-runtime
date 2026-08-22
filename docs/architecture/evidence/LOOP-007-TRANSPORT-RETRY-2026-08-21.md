# LOOP-007 — Typed transport retry execution

## Scope

This bounded slice wires the existing durable LOOP-007 retry evidence and
idempotency state to a concrete typed transport executor. The executor keeps
one caller-supplied idempotency key across all attempts, writes the durable
`started`, `unknown`, `reconciled`, and `completed` transitions through the
existing idempotency port, and records every admitted retry decision in the
configured evidence ledger.

## Guarantees

- Attempt count is a positive integer and the executor never dispatches more
  than `max_attempts` attempts.
- Only `TransportFailure` carries retry classification metadata. Any other
  exception is recorded as an unknown outcome and fails closed without a
  blind replay.
- An unknown outcome invokes the typed transport reconciliation method before
  another effectful attempt. `committed` returns the reconciled result;
  `in_flight` and `unknown` stop execution; only `retry_safe` permits replay.
- Cancellation is checked before dispatch and around the bounded backoff
  callback, so cancellation prevents the next transport effect.
- A completed or reconciled durable idempotency record is returned without a
  new transport call, preserving replay idempotency across executor/process
  recreation.

## Verification

Focused command:

```text
python -m pytest -q --basetemp .pytest-loop007-transport tests/test_loop_007_transport_retry.py
```

Result: **6 passed**.

Additional checks:

- `python -m compileall -q sonder_runtime tests`
- `python scripts/check_architecture.py`
- `python scripts/check_evidence_documents.py`
- `git diff --check`

The transport executor does not modify child providers, jobs, terminal
execution, filesystem, HTTP, MCP, session, tool audit, memory, training,
data, evaluation, update, operations, model, compaction, or selfmod code.
