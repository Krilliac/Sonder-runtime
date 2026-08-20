# WP2 LOOP-006 — Cancellation tree

This slice adds a shared cancellation-tree boundary without changing the
existing `sonder_runtime.domain.cancellation_policy` API or root compatibility
modules.

## Contract

- `CancellationNode` is a thread-safe cancellation scope. A node may have
  children, and cancellation propagates recursively to existing children.
- A child created after its parent is cancelled inherits the parent's first
  cancellation reason and cancelled state.
- Cancellation is idempotent. The first request wins and its reason remains
  stable for status/reporting.
- `CancellationStatus` distinguishes active, cancellation requested, and
  quiescent scopes. A cancellation request is immediate; quiescence is not.
- `acquire()` returns a counted lease for in-flight work. `join()` completes
  only after the cancelled node and every descendant have released their leases.
- The application layer exposes `CancellationTree`, an ID-addressable owner of
  the root scope for streams, tools, subprocesses, subagents, jobs, training,
  updates, and verification adapters.

## Boundaries and follow-up

The tree is deliberately independent of transports and workers. Adapters should
accept a node or lease and cooperate with `cancelled`/`wait()`; they must release
leases in `finally` paths. Wiring each LOOP-006 capability seam into this tree,
plus durable cancellation events and adapter-specific conformance tests, remains
follow-up work.

## Verification

Focused coverage is in `tests/test_cancellation_tree.py` and covers propagation,
late-child inheritance, idempotency, first-reason retention, status transitions,
and descendant quiescence joins.
