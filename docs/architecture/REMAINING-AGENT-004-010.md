# Remaining AGENT-004/005/008/009/010 — integration foundation

## Scope

`sonder_runtime.application.agents.lineage_delegation` is the application
boundary for integrating legacy agent modes with the WP5 registry and
continuable-subagent contracts. It supplies records and validation; it does
not dispatch workers, mutate workspaces, or persist state itself.

## Contracts

- `complete_builtin_presets()` exposes all six domain roles, including the
  previously unlisted `integrator` preset, in deterministic role order.
- `LineageRecord` preserves root, parent, child, depth, role, and explicit
  workspace assignment before dispatch.
- `WorkspaceAssignment` requires every write root to be listed as a read root;
  there is no inferred or ambient write access.
- `DelegationRequest` binds the prompt, preset, lineage, workspace, and
  evidence tags into one immutable envelope; `delegation_digest` supports
  idempotency and correlation.
- `ResultEvidence` records terminal status, output digest, verification,
  artifacts, failure reason, and usage in a bounded structured envelope.
- `RoleWorkflowGraph` provides a deterministic, cycle-rejected role graph.
  The default path is explorer → architect → editor → verifier → reviewer →
  integrator.

## Evidence

`tests/test_remaining_agent_integration.py` covers the complete preset catalog,
lineage/workspace binding, write-root rejection, result evidence requirements,
stable delegation digests, topological workflow ordering, and cycle rejection.

Focused validation:

```text
python -m pytest tests/test_remaining_agent_integration.py
python scripts/check_architecture.py
python scripts/check_requirement_evidence.py
python -m compileall -q sonder_runtime
git diff --check
```

No formal specification checkbox, commit, or push is part of this slice.
