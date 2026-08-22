# WP1 Two-Hundred-Forty-Ninth Slice — launcher output policy boundary

## Boundary

Moved the pure launcher output-tail, timeout normalization, and operation
retention policies into `sonder_runtime.adapters.launcher_output`.
`sonder_launcher.py` preserves the historical private helper names as
identity-preserving compatibility aliases, along with its existing public
constants and call sites.

Process-tree discovery/termination, operation persistence, HTTP handling, and
health-token provisioning remain launcher-owned because they retain resource,
process, or lifecycle ownership. No thread or storage boundary changes in this
slice.

## Evidence

- `tests/test_launcher_output_boundary.py` verifies packaged policy ownership,
  root alias identity, byte/text output behavior, tail bounding, and safe
  timeout/retention limits.
- `python -m pytest -q tests/test_launcher_output_boundary.py tests/test_launcher_application_boundary.py tests/test_launcher_idempotency_boundary.py`
- `python scripts/check_architecture.py`
- `python scripts/check_requirement_evidence.py`
- `python -m compileall -q sonder_runtime sonder_launcher.py`
- `git diff --check`
