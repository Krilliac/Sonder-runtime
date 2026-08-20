# WP1 One-Hundred-Eighty-Second Slice

## Boundary

Moved display-adapter vendor normalization from the root `sonder_hardware.py`
implementation into the packaged platform module
`sonder_runtime.platform.hardware_identity`. The root private helper remains a
thin compatibility delegate for existing callers.

## Evidence

- `tests/test_hardware_identity.py` verifies packaged ownership, PCI/display
  vendor recognition, conservative unknown handling, and root compatibility.
- `python scripts/check_architecture.py` passes.
- `python scripts/check_requirement_evidence.py` passes.
- `python -m compileall -q sonder_runtime sonder_hardware.py` passes.
- `git diff --check` passes.

## Scope

This slice does not modify `server.py` or the slice-181 boundary.
