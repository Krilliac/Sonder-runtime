# WP1 One-Hundred-Seventy-Eighth Slice: Doctor Status Policy Boundary

## Boundary

Moved the pure doctor-check status coercion policy from the root
`sonder_doctor` module into `sonder_runtime.domain.doctor_status`. The root
module retains an identity-preserving compatibility alias for existing doctor
report code and callers. This slice does not modify `server.py` or the
reserved slice-177 boundary.

## Evidence

- `tests/test_doctor_status.py` verifies domain ownership, canonical values,
  supported synonyms, boolean handling, and fail-closed behavior.
- Existing doctor report tests continue to exercise the consuming path through
  the compatibility alias.
- `python scripts/check_architecture.py` passes with zero violations.
- `python scripts/check_requirement_evidence.py` passes.
- `python -m compileall -q sonder_runtime server.py` passes.
- `git diff --check` passes.
