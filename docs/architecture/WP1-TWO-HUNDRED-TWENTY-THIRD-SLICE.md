# WP1 Two-Hundred-Twenty-Third Slice

## Boundary

Moved self-heal finding classification and doctor-result formatting from the
root `sonder_doctor.py` surface into
`sonder_runtime.bootstrap.doctor_checks.summarize_self_heal`. The root doctor
module retains only compatibility wiring for the legacy inspection collaborator
and `SONDER_DB` environment selection; the packaged bootstrap boundary owns the
read-only result policy and never invokes repairs.

## Evidence

- `tests/test_bootstrap_doctor_checks.py` verifies clean, repairable, mixed, and
  inspection-failure outcomes plus collaborator path forwarding.
- `python scripts/check_architecture.py` passes.
- `python scripts/check_requirement_evidence.py` passes.
