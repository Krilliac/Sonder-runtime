# WP1 Two-Hundred-Fortieth Slice — packaged doctor formatting boundary

## Boundary

Moved the remaining pure `sonder doctor` terminal formatting and status-rollup
policy from root `sonder_doctor.py` into
`sonder_runtime.bootstrap.doctor_formatting`. The root module remains the
compatibility and composition surface: `render_report`, status constants,
severity tables, and `rollup_status` retain identity-preserving aliases.
The packaged CLI now consumes the formatter and failure status directly;
check discovery, execution, and production probes remain in the root doctor
compatibility surface for this bounded slice.

## Evidence

- `tests/test_doctor_formatting.py` verifies packaged ownership, aliases,
  skipped-neutral rollup, failure precedence, and the plain-text rendering
  contract.
- `python -m pytest tests/test_doctor_formatting.py tests/test_sonder_doctor.py tests/production/test_entrypoint.py -q`
- `python scripts/check_architecture.py`
- `python scripts/check_requirement_evidence.py`
- `python -m compileall -q sonder_runtime sonder_doctor.py`
- `git diff --check`
