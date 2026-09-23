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

Reservations carry a process-scoped owner nonce plus host/PID evidence. A
different provider process cannot consume a reservation while the old owner is
live or its liveness is unresolved. After a crash, a new owner may reuse only a
`CREATED` reservation whose recorded process is proved dead; `RUNNING` work
still requires the existing explicit recovery path. Terminal verification is
persisted in the child session and projected by the adapter. Start, progress,
retry, and resume remain owned by `DurableContinuationService`, so this adapter
does not expose a second lifecycle state machine. The standalone
`SQLiteWorkerRegistry` remains
available for its compatibility contract tests, but is not composed into the
application runtime and is not an independent source of active-worker truth.

Focused verification:

```text
python -m pytest -q tests/test_worker_registry.py
python -m pytest -q tests/test_continuation_worker_registry.py
python scripts/check_architecture.py
```

The durable continuation service now reuses a matching terminal child before
any new admission or runner spawn, including after a fresh service instance
opens the same repository. A changed prompt, metadata, budget, resume key, or
idempotency key is rejected; a terminal record marked `recovery_required`
still requires the explicit resume path. The reuse handle reads the persisted
terminal result, so the repository remains the only worker truth.

Stable-key retries with a new child ID now search terminal rows as well as
active rows through one bounded durable lookup. Multiple rows for one parent
and stable key are treated as ambiguous and fail closed. Terminal reuse also
requires persisted owner, workspace, consent, and session metadata to match
the current `OperationContext`; missing proof or a foreign principal cannot
read the persisted result. Distinct resume/idempotency keys remain available
for intentional parallel lanes.

Additional verification:

```text
python -m pytest -q tests/test_continuation_worker_registry.py
18 passed
```
