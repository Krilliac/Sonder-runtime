# Worker registry foundation — 2026-09-22

This slice adds a durable SQLite worker launch contract and lifecycle registry
for the #510 worker backlog. It persists role, model/backend, effort, scope,
allowed tools, budgets, retry policy, parent, resume/idempotency keys, progress,
and terminal verification with revision compare-and-set updates. Active launches
sharing a stable key are rejected atomically; failed and interrupted records can
be reopened for a bounded resume.

The registry is deliberately an explicit foundation. It is not yet wired into
`DurableContinuationService`: the child-session repository and worker registry
are separate SQLite stores, so independent writes cannot provide atomic
launch/start/checkpoint/finish semantics. A follow-on integration must either
share the child repository transaction or add a durable prepared-mutation and
reconciliation protocol before claiming end-to-end worker durability.

Focused verification:

```text
python -m pytest -q tests/test_worker_registry.py
python scripts/check_architecture.py
```
