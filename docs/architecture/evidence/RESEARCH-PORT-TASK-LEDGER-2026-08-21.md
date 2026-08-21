# Research port: bounded task-ledger projection

Date: 2026-08-21  
Branch: `agent/port-research-findings`

Magentic-One and CrewAI both make workflow progress a first-class object:
the manager or flow owns a task graph, tracks status and dependencies, and
can explain replanning. Sonder already owns durable task/checklist records,
so the compatible port is a pure projection rather than a second workflow
engine.

`domain.task_ledger` now provides a bounded `TaskLedger` contract with:

- deterministic task ordering and dependency references;
- explicit owner, parent, status, and replan metadata;
- missing-dependency and self-edge rejection;
- canonical JSON and SHA-256 identity;
- `TaskService.task_ledger()` composition over the existing repository port.

The projection does not mutate tasks or grant execution authority. It remains
an operator-visible planning/output surface; durable task events remain the
source of truth.

Sources:

- <https://learn.microsoft.com/en-us/semantic-kernel/frameworks/agent/agent-orchestration/magentic>
- <https://docs.crewai.com/index>

Evidence: `tests/test_task_ledger.py`.
