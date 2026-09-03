# WP1 Three-Hundred-Thirty-Third Slice — verified code-repair persistence

## Boundary

The compare-and-swap persistence of a verified code repair
(`_persist_verified_code_repair`) now lives in
`sonder_runtime/adapters/code_repair_persistence.py` as
`persist_verified_code_repair`, with the input validation, the summed token
usage, the provenance labels and the failure handling unchanged. It writes
the memory database through the packaged memory store, so the adapters layer
is its home. The database opener is injected; `server.py` keeps the root
name as a thin delegate passing `_open_db` at call time, so the existing
database and persistence monkeypatch seams keep working.

## Evidence

- `tests/test_code_repair_persistence_boundary.py` verifies input refusals before any database access, the swapped-in repair with summed usage and each provenance label, database and CAS failures reported as not persisted, and the root delegate's database seam.
- `python -m pytest -q tests/test_code_repair_persistence_boundary.py tests/test_server_helpers.py -k 'boundary or repair'`
- `python scripts/check_architecture.py`
- `python scripts/check_error_signals.py`
- `python -m compileall -q sonder_runtime server.py`
- `git diff --check`
