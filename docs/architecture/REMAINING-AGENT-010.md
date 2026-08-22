# AGENT-010 — multi-role architect/editor/reviewer workflow integration

## Scope

`sonder_runtime.application.agents.workflow_integration` provides the
application orchestration seam for the built-in explorer, architect, editor,
verifier, reviewer, and integrator roles. It advances immutable workflow state
and result evidence; child execution remains behind `SubagentProvider`, and
durable parent/child records remain owned by the provider's durable lineage
adapter.

## Contracts

- `AgentWorkflowService` routes each role through its existing typed preset and
  `DelegationService`; no role can bypass budget or workspace enforcement.
- Each successive delegation uses the prior child as its parent, preserving
  the durable lineage chain and explicit root/workspace assignment.
- `AgentWorkflowState` records active delegation, monotonic revision, role
  results, and terminal success/failure state.
- `WorkflowStepResult` stores the structured `ResultEvidence` produced by the
  existing delegation boundary. Failed, cancelled, and timed-out children
  terminate the workflow without dispatching a later role.
- The default independently routed order is explorer → architect → editor →
  verifier → reviewer → integrator; callers may provide a validated role
  subset for bounded workflows.

## Evidence

`tests/test_remaining_agent_010.py` directly covers the complete role path,
preset routing, successive durable lineage parent binding, evidence-backed
terminal state, failure short-circuiting, stale-result rejection, and
fail-closed workspace context enforcement.

Focused validation:

```text
python -m pytest -q tests/test_remaining_agent_010.py
python scripts/check_architecture.py
python scripts/check_requirement_evidence.py
python -m compileall -q sonder_runtime
git diff --check
```

The master checklist and audit are intentionally unchanged by this slice.
