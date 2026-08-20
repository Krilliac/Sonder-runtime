# WP1 Eighty-Seventh Slice: Queued-action Path Boundary

## Change

`sonder_runtime.adapters.persistence.queued_actions.database_path()` now
resolves the queue database through `sonder_runtime.platform.paths` instead of
importing the root `sonder_paths` module directly.

The database filename and `SONDER_QUEUED_ACTION_DB` override are unchanged.
The queue's SQLite schema, migration dispatch, migration ledger, and immutable
`migrations/queued_actions/0001_baseline.py` remain unchanged.

## Evidence

- The focused path-boundary regression verifies the exact filename and
  environment-variable contract.
- The existing queue lifecycle and file-store migration tests cover queue
  behavior and the schema ledger.
- Compile, architecture, requirement-evidence, and staged/working diff checks
  pass.

## Boundary result

This removes one live persistence caller from the root platform dependency
without changing queue or migration semantics. The root `queued_actions.py`
compatibility boundary remains governed by the immutable-migration policy.
