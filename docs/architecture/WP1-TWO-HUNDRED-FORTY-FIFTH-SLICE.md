# WP1 Two-Hundred-Forty-Fifth Slice — hardware probe classification boundary

## Boundary

Moved the remaining root `sonder_hardware` probe-classification helpers into
the packaged platform identity boundary. The root `_vendor_from_text` and
`_looks_integrated` names remain identity-preserving compatibility aliases, so
callers retain the historical private names while policy ownership is now
packaged.

The accelerator and host-platform probe seams remain packaged at their existing
boundaries: accelerator runtime probing under
`sonder_runtime.adapters.accelerators`, and host platform probing under
`sonder_runtime.platform`. Accelerator inventory, filesystem text reading, and
model sizing are explicitly outside this slice.

## Evidence

- `tests/test_hardware_identity.py` verifies direct root-to-package identity
  for vendor and integrated/discrete classification, normalized records, and
  the packaged accelerator/platform probe seams.
- `python -m pytest tests/test_hardware_identity.py tests/test_hardware_probe_platform.py tests/test_hardware_inventory.py tests/test_sonder_hardware.py -q`
  passes.
- `python scripts/check_architecture.py` passes with zero violations.
- `python scripts/check_requirement_evidence.py` passes.
- `python -m compileall -q sonder_runtime sonder_hardware.py` passes.
- `git diff --check` passes.
