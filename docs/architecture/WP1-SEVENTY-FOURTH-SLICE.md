# WP1 Seventy-Fourth Slice: Version Boundary Caller Migration

Status: implemented on `agent/wp1-execution-status`.

## Scope

The packaged backup adapter now imports build identity through
`sonder_runtime.platform.version`, the canonical packaged platform boundary.
That boundary re-exports the existing `sonder_version` implementation, so
version values, stamped-build lookup, commit identity, and backup manifest
metadata remain unchanged. The root `sonder_version` module remains the
implementation and compatibility boundary for release tooling and legacy
callers.

This slice changes one packaged caller and adds focused identity regressions.
`server.py`, persistence, the command catalog, launchers, HTTP/REPL interfaces,
and `strangler_services.py` are unchanged.

## Evidence

- `tests/test_version_boundary.py`: focused boundary identity tests pass.
- `python -m pytest -q tests/test_version_boundary.py tests/test_backup_service.py`.
- `python -m compileall -q sonder_runtime server.py`: passes.
- `scripts/check_architecture.py`: passes.
- `scripts/check_requirement_evidence.py`: passes.
- `git diff --cached --check` and `git diff --check`: pass.

## Remaining boundary

`sonder_runtime.platform.version` still re-exports the root implementation.
The root `sonder_version` allowance cannot be removed until release metadata
ownership and all remaining packaged callers are migrated without changing
build or version semantics.
