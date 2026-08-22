# WP1 Eighty-Fifth Slice: autopilot persistence path seam

`sonder_runtime.adapters.persistence.autopilot_store` now resolves its
database location through `sonder_runtime.platform.paths` instead of directly
importing the root `sonder_paths` module.

The database contract is unchanged: `SONDER_AUTOPILOT_DB` still overrides the
default location, the default remains under the per-user Sonder state home, and
schema creation, migrations, locking, and SQLite behavior are untouched.

## Evidence

- `tests/test_autopilot_store_paths.py`
- autopilot persistence and controller regression tests
- `python -m compileall -q sonder_runtime server.py`
- `python scripts/check_architecture.py`
- `python scripts/check_requirement_evidence.py`
- `git diff --cached --check`
- `git diff --check`
