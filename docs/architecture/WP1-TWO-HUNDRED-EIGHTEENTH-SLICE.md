# WP1 Two-Hundred-Eighteenth Slice

## Boundary

Moved normalized accelerator-record construction from the root hardware
module into `sonder_runtime.platform.hardware_identity` as
`accelerator_record`. The root `sonder_hardware._accelerator` export remains a
compatibility alias, so existing probe and recommendation callers retain their
behavior while record ownership is explicit in the packaged platform layer.

## Evidence

- `tests/test_hardware_identity.py` verifies packaged ownership, compatibility
  identity, normalization, and conservative handling of unknown values.
- Existing `tests/test_sonder_hardware.py` continues to exercise the record
  through the hardware detection and recommendation flows.
- `python scripts/check_architecture.py` passes.
- `python scripts/check_requirement_evidence.py` passes.
