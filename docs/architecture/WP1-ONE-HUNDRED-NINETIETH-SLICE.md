# WP1 One-Hundred-Ninetieth Slice

## Boundary

The bounded presentation policy for fixed toolchain status-probe output now
lives in `sonder_runtime.platform.toolchain_policy.safe_output`. It owns
whitespace normalization, credential redaction, and the hard output limit.

The root `toolchain_status._safe_output` function remains a compatibility
delegate and passes its legacy configurable limit to the packaged owner.

## Evidence

- `tests/test_toolchain_output_policy.py` verifies packaged ownership,
  redaction, truncation, and legacy-limit compatibility.
- `tests/test_toolchain_status.py` covers the complete fixed-argument probe
  adapter.
- `python scripts/check_architecture.py` passes.
- `python scripts/check_requirement_evidence.py` passes.
- `python -m compileall -q sonder_runtime toolchain_status.py` passes.
- `git diff --check` passes.
