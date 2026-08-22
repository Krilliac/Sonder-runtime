# WP1 Ninety-Sixth Slice: Lifecycle Version Boundary

`sonder_runtime.adapters.web.lifecycle` now reads build identity through
`sonder_runtime.platform.version`, the canonical packaged platform boundary.
The existing `sonder_version` module attribute remains as a compatibility
alias to that packaged boundary, so lifecycle metrics and version payloads
retain the exact version, commit, and stamped-build behavior.

## Evidence

- `tests/test_version_boundary.py` covers module identity and lifecycle build
  payload behavior.
- `python -m pytest -q tests/test_version_boundary.py`.
- `python -m compileall -q sonder_runtime server.py`.
- `python scripts/check_architecture.py`.
- `python scripts/check_requirement_evidence.py`.
- `git diff --cached --check` and `git diff --check`.

The root `sonder_version` implementation remains required by release tooling
and other compatibility callers; this slice only moves one packaged caller.
