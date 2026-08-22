# WP1 process-risk ownership consolidation evidence

## Scope

Retired the root `process_risk.py` compatibility path. The packaged adapter
is now the sole implementation and all production/test imports use it directly.
Artifact-risk and native MCP files were outside this change.

## Ownership decision

`sonder_runtime/adapters/process_risk.py` is the sole implementation. The root
module is deleted and permanently listed in `RETIRED_ROOT_MODULES`, so a future
root reintroduction fails the architecture checker. Server dispatch retains the
same module-level `process_risk_module` behavior and the packaged import
preserves the public API and security contract.

## Security compatibility

The packaged implementation remains opt-in and bounded. It preserves the exact
environment gate, process/region/query/byte/time limits, read-only Windows
access rights, aggregate indicator output, and content-free result contract.
No root alias or second process implementation remains.

Focused process-risk tests verify exact opt-in fail-closed behavior, bounded
security behavior, direct packaged ownership, root absence, and the retired-root
ratchet.

## Verification

Command: `python -m pytest -q --basetemp .pytest-wp1-process-risk tests/test_process_risk.py tests/test_process_risk_server.py tests/test_process_risk_compatibility.py`

Result: pass - 31 tests passed in 2.55s using the workspace-local pytest
temporary directory `.pytest-wp1-process-risk`.
