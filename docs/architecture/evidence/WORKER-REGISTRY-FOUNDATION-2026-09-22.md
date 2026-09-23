# Worker registry foundation — 2026-09-22

This slice adds a durable SQLite worker launch contract and lifecycle registry
for the #510 worker backlog. It persists role, model/backend, effort, scope,
allowed tools, budgets, retry policy, parent, resume/idempotency keys, progress,
and terminal verification with revision compare-and-set updates. Active launches
sharing a stable key are rejected atomically; failed and interrupted records can
be reopened for a bounded resume.

The production delegation path now composes
`ContinuationWorkerRegistry` over the existing durable child-session
repository. It does not open a second active-worker store. `DelegationService`
admit-reserves the child in `durable_child_session`, and
`DurableContinuationService.spawn` consumes that exact reservation before
starting a runner. The retained metadata includes the role, model/backend,
effort, workspace scope, tools, budgets, owner, retry policy, and both stable
keys. SQLite performs the atomic duplicate-key check; the provider performs the
final budget and lineage admission under its existing CAS state transition.

Reservations carry a process-scoped owner nonce. A different provider process
cannot consume a reservation through ordinary `spawn`; restart recovery remains
an explicit recovery operation. The standalone `SQLiteWorkerRegistry` remains
available for its compatibility contract tests, but is not composed into the
application runtime and is not an independent source of active-worker truth.

Focused verification:

```text
python -m pytest -q tests/test_worker_registry.py
python -m pytest -q tests/test_continuation_worker_registry.py
python scripts/check_architecture.py
```
