# WP1 Ninety-Seventh Slice: Canonical Path Ownership

The implementation of `sonder_paths` now lives in
`sonder_runtime.platform.paths`. The root `sonder_paths.py` file is a thin
module-identity compatibility alias for release tooling and legacy callers.

The migration preserves the public path helpers, environment overrides,
default path results, and the historical `sonder_paths is
sonder_runtime.platform.paths` import identity. Legacy `memory.db` discovery
still resolves beside the repository-level compatibility boundary, while
packaged runtime state remains under the configured Sonder home.

## Evidence

- `tests/test_paths_ownership.py` verifies module identity, public symbol
  identity, thin-shim ownership, and environment overrides.
- `tests/test_sonder_paths.py` retains the existing path and migration coverage.
- `python -m compileall -q sonder_runtime server.py`.
- `python scripts/check_architecture.py`.
- `python scripts/check_requirement_evidence.py`.
- `git diff --cached --check` and `git diff --check`.

The root alias remains intentionally because release tooling and legacy
installations import `sonder_paths`; the package no longer depends on that
root implementation or needs a `sonder_paths` platform allowance.
