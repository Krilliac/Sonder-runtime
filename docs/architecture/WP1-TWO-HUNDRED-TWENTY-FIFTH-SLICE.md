# WP1 Two-Hundred-Twenty-Fifth Slice — process identity policy ownership

## Boundary

Moved live-process fingerprint selection into
`sonder_runtime.adapters.process_liveness.process_identity`. The
`ProcessProbeAdapter.identity` port method remains the public compatibility
surface, but now delegates dead/unknown/fingerprint policy to the canonical
process-liveness adapter. Platform-specific probing and the existing tri-state
`probe_process` contract are unchanged.

This slice is limited to the toolchain/process seams. It does not change
launcher orchestration, health reporting, process termination, or root
compatibility owners.

## Evidence

- `tests/test_process_probe_ownership.py` verifies canonical fingerprint policy
  and adapter delegation.
- `python -m pytest -q tests/test_process_probe_ownership.py tests/test_legacy_process_probe.py tests/test_process_liveness.py` passes.
- `python scripts/check_architecture.py` passes with zero violations.
- `python scripts/check_requirement_evidence.py` passes.
- `python -m compileall -q sonder_runtime` passes.
- `git diff --check` passes.
