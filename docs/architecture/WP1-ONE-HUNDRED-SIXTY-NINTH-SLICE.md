# WP1 One-Hundred-Sixty-Ninth Slice: Running-Source Commit Ownership

## Boundary

Moved the import-time running-source commit probe from root `server.py` into
`sonder_runtime.platform.version`, alongside the packaged runtime's existing
build-identity implementation. The root helper remains a compatibility
delegate and retains the same best-effort empty-marker behavior when Git
metadata is unavailable.

## Evidence

- `tests/test_running_source_commit_platform.py` covers successful HEAD
  discovery, Git failure fallback, and the root compatibility delegate.
- `python -m pytest tests/test_running_source_commit_platform.py -q` passes.
- `python scripts/check_architecture.py` passes with zero violations.
- `python scripts/check_requirement_evidence.py` passes.
- `python -m compileall -q sonder_runtime server.py` passes.
- `git diff --check` passes.
