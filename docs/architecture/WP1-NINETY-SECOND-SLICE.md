# WP1 Ninety-Second Slice: Update path boundary

The update engine now resolves its default release directory and active
release pointer through `sonder_runtime.platform.paths`. This removes its
direct production dependency on the root `sonder_paths` module while keeping
the platform implementation and all environment overrides unchanged.

The slice is intentionally limited to the two default path callers. Offline
bundle discovery, trust verification, signed-update checks, bootstrap
execution, activation, rollback, and release metadata remain unchanged.

Evidence:

- `tests/test_path_portability.py`
- `tests/production/test_update_engine.py`
- `python -m compileall -q sonder_runtime server.py`
- `python scripts/check_architecture.py`
- `python scripts/check_requirement_evidence.py`
- `git diff --cached --check`
- `git diff --check`
