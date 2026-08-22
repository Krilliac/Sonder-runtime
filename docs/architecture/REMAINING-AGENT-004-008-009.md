# AGENT-004/008/009 — typed presets, isolated workspaces, and delegation integration

This slice completes the application-side contracts for built-in agent
selection, child workspace isolation, and structured delegation over the
existing `SubagentProvider` and `EventSink` ports. It does not alter the master
checklist or the requirement audit.

## Implementation

- `application.agents.presets` now exposes a typed, deterministic catalog with
  `general`, `code`, `plan`, `reviewer`, `researcher`, and `build-test` presets.
  The existing canonical role catalog remains one preset per domain role.
- `WorkspaceAssignment.guard()` resolves concrete paths before containment
  checks. Reads and writes require explicit roots; writes must be under both a
  declared write root and a declared read root. No ambient or prefix-based
  access is accepted.
- `DelegationService` translates a validated delegation envelope to the
  existing `SubagentRequest` port, carries the bounded preset budget and
  workspace metadata, enforces containment in the parent `OperationContext`,
  emits an accepted event, and converts terminal provider results into the
  bounded `ResultEvidence` envelope.

## Evidence

`tests/test_remaining_agent_004_008_009.py` covers typed researcher resolution,
read/write containment, parent-context isolation, port request metadata,
accepted-event publication, result evidence integration, and lineage mismatch
rejection.

Focused validation:

```text
python -m pytest -q tests/test_remaining_agent_004_008_009.py
python scripts/check_architecture.py
python scripts/check_requirement_evidence.py
python -m compileall -q sonder_runtime
git diff --check
```
