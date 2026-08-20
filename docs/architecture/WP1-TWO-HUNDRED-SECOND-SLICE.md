# WP1 Two-Hundred-Second Slice — total-RAM probe ownership

## Boundary

Moved the stdlib-only total physical RAM probe from `sonder_hardware` into the
packaged `sonder_runtime.platform.hardware_probe` boundary. The legacy private
`sonder_hardware._probe_total_ram_gb` name remains an identity-preserving alias,
so injected hardware-probe call sites and compatibility tests retain behavior.
This slice does not modify `server.py` or the adjacent slice-201 boundary.

## Evidence

- `tests/test_hardware_probe_platform.py` verifies canonical ownership, POSIX
  page-total normalization, and fail-closed behavior when host probing fails.
- `python scripts/check_architecture.py` passes with zero violations.
- `python scripts/check_requirement_evidence.py` passes.
- `python -m compileall -q sonder_runtime sonder_hardware.py` passes.
- `git diff --check` passes.
