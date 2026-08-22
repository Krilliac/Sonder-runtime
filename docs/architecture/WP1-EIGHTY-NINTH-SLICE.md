# WP1 Eighty-Ninth Slice: migration operations-path seam

`sonder_runtime.adapters.persistence.migrations._operations_db_path()` now
consumes the canonical `sonder_runtime.platform.paths` boundary. This removes
one direct packaged caller of the root `sonder_paths` module.

The change deliberately does not alter `MIGRATIONS_ROOT`, immutable migration
module loading, `store_db_paths()` database filenames, environment overrides,
or migration ledger behavior. The remaining direct path calls in the registry
stay root-backed until their store-specific compatibility boundaries can be
migrated independently.

## Evidence

- `tests/test_migrations_paths_boundary.py`
- `python -m pytest -q tests/test_migrations_paths_boundary.py tests/production/test_migrations.py`
- `python -m compileall -q sonder_runtime server.py`
- `python scripts/check_architecture.py`
- `python scripts/check_requirement_evidence.py`
- `git diff --cached --check`
- `git diff --check`
