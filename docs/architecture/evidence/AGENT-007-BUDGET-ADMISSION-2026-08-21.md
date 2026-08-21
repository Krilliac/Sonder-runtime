# AGENT-007 — typed fleet/subagent budget admission

The typed durable subagent spawn boundary now enforces role, maximum lineage
depth, direct child count, and root-scoped concurrency before publication.
Reservations are released on publication failure and all terminal outcomes.
The durable SQLite adapter persists the new budget fields with the existing
parent and ancestor chain, preserving lineage across restart.

Focused validation:

```text
python -m pytest -q tests/test_agent007_budget_admission.py
python -m pytest -q tests/test_subagent_provider_contract.py tests/test_remaining_durable_subagents.py tests/test_remaining_agent_004_008_009.py tests/test_wp5_continuable_subagents.py tests/test_wp5_fleet_autopilot.py tests/test_runner_bound_subagent_provider.py
python scripts/check_architecture.py
python scripts/check_evidence_documents.py
python -m compileall -q sonder_runtime tests
git diff --check
```

External-provider and process-level enforcement remain outside this local
typed provider slice.
