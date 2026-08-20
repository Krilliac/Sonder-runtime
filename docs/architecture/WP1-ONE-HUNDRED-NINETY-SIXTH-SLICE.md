# WP1 One-Hundred-Ninety-Sixth Slice — CPU probe ownership

## Boundary

Moved the root `sonder_hardware._probe_cpu_count` host probe into the
canonical `sonder_runtime.platform.hardware_probe` boundary. The root module
retains an identity-preserving private compatibility alias. This slice is
limited to CPU-count probing and does not modify `server.py` or the adjacent
slice-195 boundary.

## Evidence

- `tests/test_hardware_probe_platform.py` verifies packaged ownership, the
  host value, and conservative failure handling.
- `python scripts/check_architecture.py` passes with zero violations.
- `python scripts/check_requirement_evidence.py` passes.
- `python -m compileall -q sonder_runtime sonder_hardware.py` passes.
- `git diff --check` passes.
