# WP1 One-Hundred-Seventy-Second Slice: Model-band domain boundary

## Boundary

Moved the pure model parameter-band, fit, and Q4 footprint policies from the
root `sonder_hardware` module into the existing
`sonder_runtime.domain.model_sizing` boundary established by slice 170. The
hardware recommender keeps identity-preserving compatibility exports, while
model-size policy no longer depends on the hardware probe module.

## Evidence

- `tests/test_model_sizing_domain.py` verifies canonical ownership, root
  compatibility identity, MoE sizing behavior, fit ordering, footprint
  estimation, and invalid-input handling.
- Existing `tests/test_sonder_hardware.py` coverage continues to exercise the
  recommender through the compatibility exports.
- `python scripts/check_architecture.py` passes with zero violations.
- `python scripts/check_requirement_evidence.py` passes.
- `python -m compileall -q sonder_runtime server.py` passes.
- `git diff --check` passes.
