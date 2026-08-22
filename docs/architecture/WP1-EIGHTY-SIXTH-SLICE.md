# WP1 Eighty-Sixth Slice: fleet persistence path seam

`sonder_runtime.adapters.persistence.fleet_store` now resolves both its fleet
database and principal-credential paths through
`sonder_runtime.platform.paths`. The platform module remains the canonical
packaged seam and delegates to the existing path implementation, so environment
overrides, default-home resolution, SQLite identity tracking, and migration
semantics are unchanged.

## Evidence

- `tests/test_fleet_store.py::test_fleet_paths_use_packaged_platform_seam`
- fleet-store lifecycle, restore, migration, provenance, and authentication tests
- `python -m compileall -q sonder_runtime server.py`
- `python scripts/check_architecture.py`
- `python scripts/check_requirement_evidence.py`
- `git diff --cached --check`
- `git diff --check`
