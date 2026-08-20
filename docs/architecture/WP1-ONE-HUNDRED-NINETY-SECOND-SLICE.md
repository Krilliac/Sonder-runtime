# WP1 One-Hundred-Ninety-Second Slice — doctor check-registry ownership

## Boundary

Moved the pure doctor check-registry normalization policy from
`sonder_doctor._iter_specs` into `sonder_runtime.domain.doctor_specs.iter_specs`.
The root module keeps a compatibility alias, while execution, result
normalization, and production check wiring remain outside this bounded slice.

## Evidence

- `tests/test_doctor_specs_domain.py` verifies packaged ownership, mapping and
  iterable normalization, callable naming, and invalid-entry behavior.
- `python scripts/check_architecture.py` passes.
- `python scripts/check_requirement_evidence.py` passes.
- `python -m compileall -q sonder_runtime sonder_doctor.py` passes.
- `git diff --check` passes.
