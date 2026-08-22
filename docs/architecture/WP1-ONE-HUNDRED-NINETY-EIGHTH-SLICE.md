# WP1 One-Hundred-Ninety-Eighth Slice — NPU policy ownership

## Boundary

Moved the pure NPU identification policy from
`sonder_runtime.platform.system_profile` into the dedicated
`sonder_runtime.platform.npu_policy` boundary. The system-profile module
retains identity-preserving compatibility aliases for vendor mapping, PNP
mapping, supported Linux-driver classification, and the strict device-name
matcher. This slice does not modify `server.py` or the adjacent slice-197
boundary.

## Evidence

- `tests/test_npu_policy_ownership.py` verifies packaged ownership, identity
  preservation, and conservative classification behavior.
- `tests/test_npu_profile.py` verifies the existing NPU detection flow through
  the compatibility aliases.
- `python scripts/check_architecture.py` passes with zero violations.
- `python scripts/check_requirement_evidence.py` passes.
- `python -m compileall -q sonder_runtime system_profile.py` passes.
- `git diff --check` passes.
