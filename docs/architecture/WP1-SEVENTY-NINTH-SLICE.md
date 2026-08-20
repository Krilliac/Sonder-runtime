# WP1 Seventy-Ninth Slice: NPU service path seam

The NPU service now resolves its shadow-ledger state file through
`sonder_runtime.platform.paths` instead of importing the root `sonder_paths`
module directly.

This is a behavior-preserving boundary move. The packaged platform seam still
delegates to the canonical state-path implementation, so the
`SONDER_NPU_SHADOW_LEDGER` override and normal per-user state resolution remain
unchanged.

## Evidence

- `tests/test_npu_service.py::test_shadow_ledger_uses_packaged_platform_paths`
- NPU service regression tests
- `python -m compileall -q sonder_runtime server.py`
- `python scripts/check_architecture.py`
- `python scripts/check_requirement_evidence.py`
- `git diff --cached --check`
- `git diff --check`
