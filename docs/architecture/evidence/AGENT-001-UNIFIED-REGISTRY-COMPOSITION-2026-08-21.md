# AGENT-001 Unified Registry Composition — 2026-08-21

## Scope

This slice connects the typed Fleet/Autopilot launch envelope and the typed
Workbench/review registrations through one lazy application composition path.
The path is limited to the agent-fleet boundary; execution-world,
filesystem, terminal, HTTP, MCP, jobs/session, memory, training, data,
evaluation, update, operations, model, compaction, and selfmod surfaces are
not changed.

## Implemented contract

- `UnifiedAgentRegistryService` composes `FleetAutopilotAdapter` and
  `WorkbenchReviewAdapter` over one backend.
- `AgentBudget` and `AgentLineage` are carried in the canonical launch and
  checked before backend publication. Missing budget, depth exhaustion, child
  limits, and root concurrency exhaustion fail closed.
- `FleetStoreRegistryAdapter` persists both Fleet and Autopilot envelopes in
  the durable fleet ledger. Resume is allowed only for a durable queued,
  failed, or interrupted record; interrupted records retain explicit
  `restart_required` truth.
- `Application.agent_registry` is a lazy cached factory, so construction does
  not open the fleet database or register an owner.

## Evidence

Focused command:

```text
python -m pytest -q tests/test_agent001_unified_composition.py tests/test_wp5_fleet_autopilot.py tests/test_wp5_workbench_review.py
```

Expected result: all tests pass. The tests prove one backend receives both
launch modes, Workbench review remains read-only, admission preserves budget
and lineage, restart cannot be fabricated from a running record, and a
released reservation can be reused.

Static gates:

```text
python -m compileall -q sonder_runtime tests
python scripts/check_architecture.py
python scripts/check_evidence_documents.py
git diff --check
```
