# WP1 Three-Hundred-Forty-Fourth Slice — cloud tier classification

## Boundary

Added `is_cloud_tier` to the existing
`sonder_runtime/domain/model_routing.py` alongside `is_cloud_model_name`.

Determines whether a tier name routes to cloud infrastructure by checking
set membership, then falling back to `is_cloud_model_name` on the tier's
mapped model. Parameterized (`cloud_tiers`, `tier_map`, optional `model`)
so the domain function stays free of module globals.

The root `server._is_cloud_tier` is a compatibility delegate that binds the
module-level `CLOUD_TIERS` and `TIERS` globals.

## Evidence

- `tests/test_model_routing_boundary.py` verifies identity alias,
  set membership, tier-map fallback, explicit model override, missing tier,
  and server delegate binding.
- `python -m pytest -q tests/test_model_routing_boundary.py` — 6 passed
- `python scripts/check_architecture.py` — silent, exit 0
- `python scripts/check_requirement_evidence.py` — silent, exit 0
- `python -m compileall -q sonder_runtime/domain/model_routing.py server.py` — silent, exit 0
- `git diff --check` — silent, exit 0
