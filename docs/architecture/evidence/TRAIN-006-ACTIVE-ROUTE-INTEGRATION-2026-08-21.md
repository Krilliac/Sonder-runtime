# TRAIN-006 active-route integration — 2026-08-21

## Bounded result

`AttendedRouteActivationBoundary` composes the pure typed `RoutePlanner` with
the existing `DeploymentRollbackService` and an append-only
`RouteSelectionStore`. A selection is persisted only after attended activation
passes the deployment health gate. Failed attendance or health checks therefore
leave the active route and durable selection history unchanged. Rollback uses
the existing retained-route contract and records a durable rollback selection
with its reason and prior route.

## Verification

Focused command:

```text
python -m pytest -q tests/test_train006_route_activation.py tests/test_wp7_deployment_rollback.py tests/test_spec5_route_planner.py
```

Expected evidence: activation, attendance refusal, health refusal, route
selection, and rollback tests pass. The store is an in-memory reference port;
platform-specific durable persistence and external deployment execution remain
outside this local slice and are intentionally not claimed.

Scope was limited to training route activation, its focused tests, and this
evidence document. HTTP, MCP, jobs/session, execution spill, memory,
operations, model, compaction, and selfmod surfaces were not changed.
