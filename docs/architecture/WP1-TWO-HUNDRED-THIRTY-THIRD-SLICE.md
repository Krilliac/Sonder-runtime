# WP1 Two-Hundred-Thirty-Third Slice — version commit-probe ownership

## Boundary

The unstamped `BuildInfo` fallback now reuses the packaged
`sonder_runtime.platform.version.running_source_commit_at_import` probe. This
removes the duplicate Git lookup from the build-identity path while preserving
the `_commit_from_git` compatibility helper, the root `sonder_version` module
identity, and the literal root `VERSION` assignment required by release
tooling.

Release parsing, build stamping, and all non-version platform boundaries are
outside this slice.

## Evidence

- `tests/test_version_implementation_boundary.py` verifies packaged helper
  ownership, root alias identity, successful commit fallback, and the legacy
  `unknown` marker.
- `python -m pytest tests/test_version_implementation_boundary.py -q`
- `python scripts/check_architecture.py`
- `python scripts/check_requirement_evidence.py`
- `python -m compileall -q sonder_runtime sonder_version.py`
- `git diff --check`
