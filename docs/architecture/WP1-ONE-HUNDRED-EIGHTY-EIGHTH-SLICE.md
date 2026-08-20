# WP1 One-Hundred-Eighty-Eighth Slice — skipped doctor-result ownership

## Boundary

Moved the pure skipped-check result construction policy from the root
`sonder_doctor` composition module into the existing packaged
`sonder_runtime.domain.doctor_result` boundary. The root `_skip` helper remains
as a compatibility delegate, so doctor check behavior and its output shape are
unchanged. This slice does not modify `server.py` or the reserved slice-187
boundary.

## Evidence

- `tests/test_doctor_skipped_result.py` verifies packaged ownership, the root
  compatibility delegate, and reason formatting.
- `python scripts/check_architecture.py` passes.
- `python scripts/check_requirement_evidence.py` passes.
- `python -m compileall -q sonder_runtime server.py` passes.
- `git diff --check` passes.
