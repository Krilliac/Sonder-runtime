# MODEL-008 role-budget caller enforcement — 2026-08-21

## Scope

This bounded slice closes the caller-enforcement gap at the typed
`CapabilityRouter` boundary. `RoutingRequest.requested_budget` is an immutable
`BudgetLimit` value, and the router compares each requested ceiling with the
role's existing immutable `RoleRoute.budget` before selecting a model.

## Invariants

- Planner, editor, verifier, and other roles continue to use independent
  role-owned budgets from `role_budget`.
- A caller cannot widen `steps`, `output_tokens`, or `wall_seconds`.
- Over-budget requests fail closed before a `RouteDecision` is returned.
- Capability requirements are still checked and unsupported routes remain
  rejected.
- No provider, transport, persistence, filesystem, or execution I/O is added
  to route planning.

## Evidence

Focused verification:

```text
python -m pytest -q tests/test_model008_budget_routing.py tests/test_wp7_capability_routing.py
```

The focused tests cover exact-boundary acceptance, each budget dimension's
overage, typed-input rejection, and preservation of capability admission.
