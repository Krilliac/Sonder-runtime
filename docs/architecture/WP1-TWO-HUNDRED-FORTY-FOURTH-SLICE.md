# WP1 Two-Hundred-Forty-Fourth Slice — launcher idempotency policy

## Boundary

Moved the pure launcher idempotency-key normalization and durable command
replay validation policy into
`sonder_runtime.adapters.launcher_idempotency`. `sonder_launcher.py` keeps
identity-preserving aliases for the historical private helpers and validation
regexes, so persistent operation lookup and HTTP replay behavior remain
unchanged. Process-tree ownership, liveness probing, and lifecycle execution
are outside this slice.

## Evidence

- `tests/test_launcher_idempotency_boundary.py` verifies packaged ownership,
  root alias identity, key normalization, and replay validation boundaries.
- `python -m pytest -q tests/test_launcher_idempotency_boundary.py`
- `python scripts/check_architecture.py`
- `python scripts/check_requirement_evidence.py`
- `python -m compileall -q sonder_runtime sonder_launcher.py`
- `git diff --check`
