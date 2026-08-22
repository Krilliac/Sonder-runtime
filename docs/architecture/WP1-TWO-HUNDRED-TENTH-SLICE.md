# WP1 Two-Hundred-Tenth Slice — bootstrap config loading ownership

## Boundary

Moved the best-effort optional configuration-loading seam used by the
read-only doctor checks from `sonder_doctor.py` into the packaged
`sonder_runtime.bootstrap.config_loading` boundary.  The root doctor helper
remains a compatibility delegate, while typed parsing and validation stay
owned by `sonder_runtime.platform.config`.  This slice does not modify
`server.py` or the slice-209 boundary.

## Evidence

- `tests/test_bootstrap_config_loading.py` verifies successful loading,
  import failure, loader failure, and root-delegate behavior.
- `python -m pytest tests/test_bootstrap_config_loading.py
  tests/test_doctor_ollama_catalog.py -q` passes.
- `python scripts/check_architecture.py` passes with zero violations.
- `python scripts/check_requirement_evidence.py` passes.
- `python -m compileall -q sonder_runtime sonder_doctor.py` passes.
- `git diff --check` passes.
