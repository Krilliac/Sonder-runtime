# WP1 Ninety-Fourth Slice: migration path seam

`sonder_runtime.adapters.persistence.migrations` now resolves every mutable
state path and its process lock through `sonder_runtime.platform.paths`.

The packaged seam re-exports the exact path functions from `sonder_paths`, so
the change preserves path bytes, environment overrides, lock placement, and
immutable migration replay. The migration registry still keeps the repository
level `migrations/` directory as its immutable source and still imports
`sonder_version` for the ledger's application identity; that version boundary
is intentionally a separate slice.

## Evidence

- `tests/test_migrations_paths_boundary.py`
- `tests/production/test_migrations.py`
- `python -m pytest -q tests/test_migrations_paths_boundary.py tests/production/test_migrations.py`
- `python -m compileall -q sonder_runtime server.py`
- `python scripts/check_architecture.py`
- `python scripts/check_requirement_evidence.py`
- `git diff --cached --check`
- `git diff --check`
