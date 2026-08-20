# WP1 Two-Hundred-Twelfth Slice — GPU probe ownership

## Boundary

Moved the concrete NVIDIA GPU memory probe from the root `sonder_hardware`
implementation into a packaged accelerator adapter. The root module retains
`_probe_gpu` as an identity-preserving compatibility alias, and its default
profile wiring is unchanged. This slice does not modify `server.py` or the
adjacent slice-211 boundary.

## Evidence

- `tests/test_hardware_probe_platform.py` verifies canonical ownership, largest
  reported VRAM selection, cold-driver timeout retry, and failure degradation.
- `python -m pytest tests/test_hardware_probe_platform.py -q` passes.
- `python scripts/check_architecture.py` passes with zero violations.
- `python scripts/check_requirement_evidence.py` passes.
- `python -m compileall -q sonder_runtime sonder_hardware.py` passes.
- `git diff --check` passes.
