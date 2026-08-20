# WP1 One-Hundred-Seventy-Third Slice: Cloud-model policy boundary

## Boundary

Moved the pure live cloud-model override policy out of root `server.py` into
`sonder_runtime.domain.cloud_model_policy`. The root server keeps the same
`_live_cloud_model` compatibility alias, so existing callers retain identity,
retired-model fallback, and original override spelling behavior.

## Evidence

- `tests/test_cloud_model_policy.py` verifies canonical domain ownership,
  compatibility identity, retired/empty fallback, case-insensitive matching,
  spelling preservation, and injectable provider catalogs.
- `python -m pytest tests/test_cloud_model_policy.py tests/test_server_helpers.py -q`
  passes.
- `python scripts/check_architecture.py` passes with zero violations.
- `python scripts/check_requirement_evidence.py` passes.
- `python -m compileall -q sonder_runtime server.py` passes.
- `git diff --check` passes.
