# WP1 Forty-Seventh Slice: shrink the active legacy-root policy

The architecture checker no longer grants `autopilot_store` as an active
package legacy-root import. A source audit found no production caller outside
the immutable `migrations/autopilot/0001_baseline.py` migration, which is
already covered by the narrow compatibility-import exception.

The root `autopilot_store.py` compatibility alias remains packaged for
historical migration replay. This slice changes only the architecture policy
and its regression coverage; it does not alter runtime behavior, migration
bytes, persistence implementations, launchers, HTTP, REPL, or the server.

## Evidence

- `python -m pytest -q tests/production/test_architecture.py`: passes.
- `python scripts/check_architecture.py`: passes with two active legacy-root
  allowances (`server`, `fleet_store`).
- `python -m compileall -q sonder_runtime server.py`: passes.
- `python scripts/check_requirement_evidence.py`: passes.
- `git diff --cached --check` and `git diff --check`: pass.

The remaining `autopilot_store` root is an immutable-migration compatibility
boundary, not an active package dependency. Its removal remains a separate
byte-identity migration task.
