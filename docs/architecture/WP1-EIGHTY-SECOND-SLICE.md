# WP1 Eighty-Second Slice: update-engine version seam

`sonder_runtime.adapters.updates.engine` now reads build identity through
`sonder_runtime.platform.version` rather than importing the root
`sonder_version` module directly.

This is a deliberately bounded, read-only boundary move. The platform module
re-exports the canonical `VERSION`, `BuildInfo`, and `build_info` objects, so
update status reports retain the exact release version, commit, dirty-state,
and stamped-build metadata. Bundle manifests, signature verification, staged
installation, atomic activation, rollback, and release-tool parsing are
unchanged.

## Evidence

- `tests/test_update_engine_version_boundary.py`
- update-engine and signed-manifest regression tests
- `python -m compileall -q sonder_runtime server.py`
- `python scripts/check_architecture.py`
- `python scripts/check_requirement_evidence.py`
- `git diff --cached --check`
- `git diff --check`
