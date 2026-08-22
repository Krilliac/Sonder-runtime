# WP1 Eightieth Slice: Secret rotation path boundary

The packaged secret-rotation adapter now resolves its default rotation-state
location through `sonder_runtime.platform.paths` instead of importing the root
`sonder_paths` module directly.

This is a behavior-preserving boundary move. The platform seam continues to
delegate to the existing path implementation, while `SONDER_ROTATION_STATE`
overrides, secret-file permissions, rotation contents, and expiration behavior
remain unchanged.

## Evidence

- `tests/production/test_secret_rotation.py::test_rotation_state_path_uses_packaged_platform_home`
- Secret-rotation regression tests
- `python -m compileall -q sonder_runtime server.py`
- `python scripts/check_architecture.py`
- `python scripts/check_requirement_evidence.py`
- `git diff --cached --check`
- `git diff --check`
