# Remaining model gateway: health, roles, and hardware truth

This slice closes the model-gateway coordination gap without changing either
transport adapter or the formal master-spec checklist.

## Contract

`sonder_runtime/application/model_gateway/health_and_roles.py` provides one
application-facing `ModelGatewayContract` over the existing `ModelGateway`
adapters. Providers are routable only in explicit `ready` or `degraded`
states. Logical roles bind to providers and models while `RoleBudgetBook`
keeps independent, immutable-by-snapshot budgets for each role.

## Hardware and model truth

`ModelParameters` preserves total and active parameter counts for MoE models;
total parameters describe resident weight planning and active parameters
describe per-token compute. `NpuBoundary` distinguishes inventory detection,
runtime availability, and provider binding. Detection alone never produces a
runtime-ready claim.

## Evidence

- `tests/test_remaining_model_gateway.py`
- focused command: `python -m pytest -q tests/test_remaining_model_gateway.py`
- architecture and requirement-evidence gates
- `python -m compileall -q sonder_runtime`
- `git diff --check`

No formal checkbox, commit, or push is part of this slice.
