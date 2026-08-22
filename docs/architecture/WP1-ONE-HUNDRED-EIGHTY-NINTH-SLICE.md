# WP1 One-Hundred-Eighty-Ninth Slice — model-inventory protocol ownership

## Boundary

Moved the Ollama inventory-envelope validation and row filtering policy from
root `server.py` into `sonder_runtime.adapters.model_inventory`. The root
`_inventory_rows` helper remains a compatibility delegate, preserving the
existing transport error type and malformed-row behavior.

This slice is limited to that server-owned adapter boundary and does not
modify the existing model-inventory formatting or model-capability policies.

## Evidence

- `tests/test_model_inventory_adapter.py` verifies packaged ownership,
  compatibility behavior, malformed-envelope rejection, and partial-row
  handling.
- `python scripts/check_architecture.py` passes.
- `python scripts/check_requirement_evidence.py` passes.
- `python -m compileall -q sonder_runtime server.py` passes.
- `git diff --check` passes.
