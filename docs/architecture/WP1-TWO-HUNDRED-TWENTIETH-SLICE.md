# WP1 Two-Hundred-Twentieth Slice

## Boundary

Moved the host-probe filesystem text reader from the root
`sonder_hardware` module into `sonder_runtime.platform.hardware_probe` as
`read_text`. The root `_read_text` name remains an identity-preserving
compatibility alias, while Linux accelerator probing continues to use the
same conservative strip-and-swallow behavior.

## Evidence

- `tests/test_hardware_probe_platform.py` verifies packaged ownership,
  whitespace normalization, and conservative handling of missing paths.
- Existing `tests/test_sonder_hardware.py` continues to exercise the reader
  through the accelerator discovery flow.
- `python scripts/check_architecture.py` passes.
- `python scripts/check_requirement_evidence.py` passes.
