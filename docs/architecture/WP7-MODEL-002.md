# WP7 MODEL-002 — Capability-based role routing

Status: implemented locally; focused verification recorded with this slice.

`CapabilityProfile` is a provider-neutral measured profile for a model. It
records supported planning/editing/tool/structured/verification, summarizing,
embedding, and vision capabilities, plus bounded quality, latency, context,
and escalation-rank signals. `RoleRoute` binds each WP5 role to its own
required capabilities and its own `BudgetLimit`; planner, editor, verifier,
reviewer, explorer, and integrator budgets are never combined.

`CapabilityRouter` is pure application policy. It filters profiles by the
union of role and request capabilities, selects deterministically, and only
advances to a stronger rank after high uncertainty or verifier failure. The
maximum escalation count is explicit, the reason is returned in the decision,
and exhausted routes report `can_escalate=False`. No model, network, provider,
or persistence I/O occurs in this boundary.

Focused verification: `tests/test_wp7_capability_routing.py` covers capability
rejection, deterministic selection, uncertainty/verifier triggers, bounded
escalation, and independent role budgets. Formal master-spec checkboxes were
not edited; MODEL-002/005/007/008 remain subject to the requirement-level
evidence audit.
