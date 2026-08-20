# WP1 Two-Hundred-Forty-Second Slice — hardware sizing and probe boundaries

## Boundary

Moved the remaining pure capacity-band and largest-model-class helpers used by
`sonder_hardware` into the packaged `sonder_runtime.domain.model_sizing`
boundary. The root `_band_for` and `_largest_model_class` names remain
compatibility delegates with unchanged edge behavior.

This slice also records the packaged hardware boundaries: the NVIDIA runtime
probe remains owned by `sonder_runtime.adapters.accelerators.gpu_probe`, while
host-platform probing remains owned by `sonder_runtime.platform.hardware_probe`.
Accelerator inventory, de-duplication, and host-probe text reading are
deliberately outside this slice.

## Evidence

- `tests/test_hardware_identity.py` verifies the root sizing delegates and
  packaged accelerator/platform probe ownership.
- `python -m pytest tests/test_hardware_identity.py tests/test_hardware_inventory.py tests/test_sonder_hardware.py -q` passes.
- `python scripts/check_architecture.py` passes with zero violations.
- `python scripts/check_requirement_evidence.py` passes.
- `python -m compileall -q sonder_runtime sonder_hardware.py` passes.
- `git diff --check` passes.
