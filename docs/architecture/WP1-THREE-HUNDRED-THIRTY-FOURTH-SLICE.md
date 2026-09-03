# WP1 Three-Hundred-Thirty-Fourth Slice — project scope keys

## Boundary

Moved `_project_scoped_path_key` (pure tool-name-to-path-parameter lookup) into `sonder_runtime/domain/project_scope_keys.py` as `project_scoped_path_key`. The root `_project_scoped_path_key` name is now an identity-preserving alias.

## Evidence

- `tests/test_project_scope_keys_boundary.py` verifies alias identity and known/unknown tool lookups.
- `python scripts/check_architecture.py`
- `python scripts/check_error_signals.py`
- `python -m compileall -q sonder_runtime tests server.py`
- `git diff --check`
