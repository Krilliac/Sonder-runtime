# WP1 Two-Hundred-Thirty-Second Slice — launcher lifecycle input policy

## Boundary

Moved the pure bounded `context_size` normalization policy used by launcher
start, stop, and restart requests into
`sonder_runtime.application.lifecycle`. The root `sonder_launcher.py` keeps
identity-preserving aliases for `normalize_context_size`,
`MAX_CONTEXT_TOKENS`, and the historical private regex name. Process control,
HTTP serving, persistence, configuration, logging, and headless behavior are
unchanged and outside this slice.

## Evidence

- `tests/test_launcher_application_boundary.py` verifies packaged ownership,
  root alias identity, canonicalization, and validation boundaries.
- `python -m pytest -q tests/test_launcher_application_boundary.py`
- `python scripts/check_architecture.py`
- `python scripts/check_requirement_evidence.py`
- `python -m compileall -q sonder_runtime sonder_launcher.py`
- `git diff --check`
