# REMAINING-OPS-005 — Graceful drain behavior

This slice adds `GracefulDrainCoordinator` at the application operations
boundary.  It composes the existing admission, deadline, descendant
cancellation/settle, startup-reconciliation, flush, cleanup, and platform
process-tree contracts without taking ownership of any adapter or operating
system process.

The sequence is deliberately bounded:

1. stop admission so no new work can enter;
2. announce one monotonic deadline and reason;
3. cooperatively cancel and settle descendants;
4. flush durable/application state;
5. execute only injected, bounded process-tree cleanup intents and run final
   cleanup.

`GracefulDrainResult.clean` is fail-closed.  It is true only when every
barrier returns success, the deadline has not expired, and every process-tree
receipt explicitly reports completion.  A partial receipt, missing cleanup,
exception, or deadline expiry produces an incomplete result and preserves the
unsettled evidence for the caller/supervisor.

Coverage is in `tests/test_remaining_graceful_drain.py`.
