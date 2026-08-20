# WP1 Two-Hundred-Third Slice

## Boundary

The environment-only `SONDER_ALLOW_PRIVATE_COT` opt-in policy now lives in
`sonder_runtime.platform.private_cot_policy.opt_in_enabled`. It owns the
flag's default-off and truth-value semantics while leaving the separate
permission-rule gate in `server.py` unchanged. The root
`server.private_cot_opt_in_enabled` function remains a compatibility delegate.

## Evidence

- `tests/test_private_cot_policy.py` verifies the packaged policy and root
  compatibility wrapper.
- `python scripts/check_architecture.py` passes.
- `python scripts/check_requirement_evidence.py` passes.
- `python -m compileall -q sonder_runtime server.py` passes.
- `git diff --check` passes.
