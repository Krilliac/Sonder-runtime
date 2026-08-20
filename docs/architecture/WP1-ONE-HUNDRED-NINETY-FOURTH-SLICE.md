# WP1 One-Hundred-Ninety-Fourth Slice — Hardware platform probe ownership

## Boundary

Moved the root `sonder_hardware._probe_platform` host-platform lookup into
`sonder_runtime.platform.hardware_probe.probe_platform`. The root module keeps
an identity-preserving private compatibility alias, while the packaged platform
boundary owns the operating-system probe and its safe unknown fallback.

This slice is limited to the non-server hardware platform seam. It does not
modify `server.py` or the slice-193 boundary.

## Evidence

- `tests/test_hardware_probe_platform.py` verifies canonical function identity,
  Windows platform normalization, and degradation when the host lookup raises.
- `python scripts/check_architecture.py` passes with zero violations.
- `python scripts/check_requirement_evidence.py` passes.
- `python -m compileall -q sonder_runtime sonder_hardware.py` passes.
- `git diff --check` passes.
