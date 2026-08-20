# WP1 Ninety-Fifth Slice: Packaged Entrypoint Path Boundary

`sonder_runtime.__main__` now resolves its default backup destination through
`sonder_runtime.platform.paths`. This removes the remaining dynamic root
`sonder_paths` import from the packaged entrypoint while preserving the
historical default-home resolution and explicit/configured target precedence.

## Scope

This slice changes only the packaged entrypoint's path caller and focused
boundary coverage. `server.py`, the command catalog, persistence stores,
launchers, HTTP/REPL adapters, and `strangler_services.py` are unchanged.

## Evidence

- `tests/test_runtime_entrypoint_paths_boundary.py`
- `python -m pytest -q tests/test_runtime_entrypoint_paths_boundary.py tests/test_runtime_entrypoint_version_boundary.py tests/production/test_entrypoint.py`
- `python -m compileall -q sonder_runtime server.py`
- `python scripts/check_architecture.py`
- `python scripts/check_requirement_evidence.py`
- `git diff --cached --check`
- `git diff --check`

The platform path module remains a compatibility-backed seam, so path bytes,
environment overrides, and package behavior remain unchanged.
