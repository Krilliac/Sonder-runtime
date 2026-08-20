# WP1 Ninetieth Slice: strangler memory path seam

`LegacyUnitOfWork` now resolves its default memory database through
`sonder_runtime.platform.paths`. The packaged boundary re-exports the live
path implementation, so the existing memory-store port, connection lifecycle,
and explicit `db_path` override remain unchanged.

## Evidence

- `tests/test_strangler_services_paths.py`
- `tests/test_legacy_memory_repository.py`
- `python -m compileall -q sonder_runtime server.py`
- `python scripts/check_architecture.py`
- `python scripts/check_requirement_evidence.py`
- `git diff --cached --check`
- `git diff --check`
