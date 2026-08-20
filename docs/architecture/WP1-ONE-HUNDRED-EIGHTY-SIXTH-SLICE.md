# WP1 One-Hundred-Eighty-Sixth Slice

## Boundary

Moved the pure integrated-versus-discrete accelerator classification policy
from the root `sonder_hardware.py` implementation into the packaged platform
module `sonder_runtime.platform.hardware_identity`. The root helper remains a
thin compatibility delegate. This slice is limited to classification and does
not alter hardware enumeration, memory probing, or model-sizing behavior.

## Evidence

- `tests/test_hardware_identity.py` verifies packaged ownership, Intel, NVIDIA,
  AMD, and conservative unknown classification, plus root compatibility.
- `python -m pytest -q tests/test_hardware_identity.py` passes.
- `python scripts/check_architecture.py` passes with zero violations.
- `python scripts/check_requirement_evidence.py` passes.
- `python -m compileall -q sonder_runtime sonder_hardware.py` passes.
- `git diff --check` passes.

## Scope

This slice does not modify `server.py`, the slice-185 boundary, or the existing
vendor-normalization and memory-probe boundaries.
