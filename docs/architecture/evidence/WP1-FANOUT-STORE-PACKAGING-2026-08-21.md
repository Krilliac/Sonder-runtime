# WP1 fanout-store packaging evidence — 2026-08-21

## Decision

`fanout_store.py` is a durable persistence adapter, not a root application
module. Its canonical implementation now lives at
`sonder_runtime.adapters.persistence.fanout_store`. The root module remains a
`sys.modules` identity compatibility alias, preserving legacy imports,
private inspection surfaces, and monkeypatches for `_SCHEMA`, `_connect`,
`_write_transaction`, `time`, and `sqlite3`.

`server.py` imports the packaged adapter directly. The migration preserves the
existing SQLite schema and transaction behavior, encrypted execution-prompt
storage, leases and worker ownership, result dispatch fencing, result
terminal-state lifecycle, health/cooldown tracking, retry timestamps,
cancellation uncertainty handling, stale-run reconciliation, retention
pruning, and test reset seams.

## Verification

- `tests/test_fanout_store.py`
- `tests/test_fanout_store_compatibility.py`
- `tests/test_model_fanout.py`
- Result: **265 passed**.
- `python -m compileall -q sonder_runtime/adapters/persistence/fanout_store.py fanout_store.py server.py`: passed.
- Fanout-specific architecture ownership and compatibility ratchets: passed.
- `python scripts/check_architecture.py`: the fanout boundary passes; the
  repository checker still reports the pre-existing unrelated violation in
  `sonder_runtime/application/training/hardware_planning.py`.
- `python scripts/check_requirement_evidence.py`: passed after generated
  status artifacts were refreshed.

## Boundary status

The root alias is intentionally retained and registered in
`COMPATIBILITY_ROOT_MODULES`. No Git metadata was changed by this slice.
Formal master-checklist promotion remains separate from this implementation
evidence.
