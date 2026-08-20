# WP1 One-Hundred-Seventy-Sixth Slice: Hardware Probe Normalization Boundary

## Boundary

Moved the verified pure MB/GB memory-value normalization helper out of the
root `sonder_hardware` probe module into
`sonder_runtime.platform.hardware_probe`. The root module retains a private
compatibility alias while platform hardware probing owns the normalization
policy. `server.py` and the slice-175 boundary remain untouched.

## Evidence

- `tests/test_hardware_probe_platform.py` verifies canonical ownership,
  decimal/comma parsing, unit conversion, and invalid-input behavior.
- Existing `tests/test_sonder_hardware.py` continues to cover the consuming
  Windows and macOS hardware-probe paths through the compatibility alias.
- `python scripts/check_architecture.py` passes with zero violations.
- `python scripts/check_requirement_evidence.py` passes.
- `python -m compileall -q sonder_runtime server.py` passes.
- `git diff --check` passes.
