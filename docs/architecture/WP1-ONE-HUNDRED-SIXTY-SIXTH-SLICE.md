# WP1 One-Hundred-Sixty-Sixth Slice: Configuration Check Adapter

## Boundary

Moved the read-only, already-validated configuration check factory out of the
root `sonder_doctor` module into the packaged
`sonder_runtime.adapters.config_validation` boundary. The root module retains
its compatibility function and delegates to the packaged owner. This slice is
separate from slice 165's CPU-thread environment policy migration.

## Evidence

- `tests/test_config_validation_adapter.py` verifies the packaged behavior,
  missing-configuration fallback, and root delegation seam.
- `python -m pytest tests/test_config_validation_adapter.py tests/test_sonder_doctor.py -q` passes.
- `python scripts/check_architecture.py` passes with zero violations.
- `python scripts/check_requirement_evidence.py` passes.
- `python -m compileall -q sonder_runtime server.py` passes.
- `git diff --check` passes.
