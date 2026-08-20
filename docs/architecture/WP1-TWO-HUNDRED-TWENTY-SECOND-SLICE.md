# WP1 Two-Hundred-Twenty-Second Slice

## Boundary

Moved accelerator-inventory de-duplication from the root `sonder_hardware`
module into `sonder_runtime.adapters.accelerators.inventory` as
`dedupe_accelerators`. The root `_dedupe_accelerators` name remains an
identity-preserving compatibility alias, and all Windows, Linux, and macOS
accelerator paths continue to use the same policy.

## Evidence

- `tests/test_hardware_inventory.py` verifies packaged ownership, exact device
  de-duplication, and conservative identity fallback when a device ID is absent.
- `python scripts/check_architecture.py` passes.
- `python scripts/check_requirement_evidence.py` passes.
