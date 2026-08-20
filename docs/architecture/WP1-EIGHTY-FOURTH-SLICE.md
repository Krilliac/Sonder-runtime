# WP1 Eighty-Fourth Slice: update-service version seam

`sonder_runtime.adapters.updates.service` now reads build identity through
`sonder_runtime.platform.version` rather than importing the root
`sonder_version` module directly.

The packaged platform module re-exports the canonical `VERSION`, `BuildInfo`,
and `build_info` objects. This keeps bundle manifest versioning and stamped
build metadata unchanged; signed update verification, TUF boundaries, archive
handling, staged installation, and release tooling are untouched.

## Evidence

- `tests/test_update_service_version_boundary.py`
- update-service and signed-manifest regression tests
- `python -m compileall -q sonder_runtime server.py`
- `python scripts/check_architecture.py`
- `python scripts/check_requirement_evidence.py`
- `git diff --cached --check`
- `git diff --check`
