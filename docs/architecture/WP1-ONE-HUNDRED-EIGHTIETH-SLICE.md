# WP1 One-Hundred-Eightieth Slice: Doctor Result Boundary

## Boundary

Moved the pure doctor-check result normalization policy from the root
`sonder_doctor` module into `sonder_runtime.domain.doctor_result`. The root
module retains an identity-preserving compatibility alias for existing report
assembly and callers. This slice does not modify `server.py`, the prior doctor
status boundary, or the reserved slice-179 boundary.

## Evidence

- `tests/test_doctor_result.py` verifies packaged ownership, compatibility
  identity, mapping and tuple normalization, string conversion, and
  fail-closed scalar handling.
- Existing doctor report tests continue to exercise the consuming path through
  the compatibility alias.
- `python scripts/check_architecture.py` passes with zero violations.
- `python scripts/check_requirement_evidence.py` passes.
- `python -m compileall -q sonder_runtime server.py` passes.
- `git diff --check` passes.
