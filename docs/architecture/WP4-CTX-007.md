# WP4 CTX-007 — Context overflow recovery

This slice adds `sonder_runtime.application.context_overflow_recovery`, an
application-level orchestration policy. Existing context construction,
classification, and compaction modules remain unchanged.

## Contract

1. **Compact before overflow.** `prepare()` compares estimated prompt plus
   reserved output against a configurable safety boundary (90% by default).
   It invokes the supplied compactor before the provider is expected to reject
   the request.
2. **Adaptive recovery is bounded.** `recover()` retries only a proven
   overflow. It tries one compacted candidate, then at most eight configured
   shrink steps (three by default), with a monotonically decreasing factor.
   Non-overflow failures are returned without retry.
3. **Last-good behavior.** Callers publish a complete successful view with
   `accept()`. Snapshots are deep copies and are returned as deep copies. If
   every bounded candidate fails, recovery returns that snapshot; without one,
   it returns `unrecoverable`.

The module is callback-driven so token accounting and payload-specific
compaction remain owned by their existing boundaries. It has no provider,
filesystem, network, or model dependency.

## Verification

```text
python -m pytest -q tests/test_context_overflow_recovery.py
python -m pytest -q tests/test_context_overflow.py tests/test_context_compaction_boundary.py tests/test_context_overflow_recovery.py
```
