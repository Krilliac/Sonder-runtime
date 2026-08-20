# WP3 SEAM-010 — JobRegistry / WorkflowEngine contract

## Boundary

`sonder_runtime.application.ports.jobs` defines the provider-neutral contract
for durable background jobs and resumable workflows. `JobIdentity` carries the
stable job, operation, kind, idempotency, parent-job, and parent-session
identity; it is not regenerated when a worker restarts.

`JobRegistryService` exposes the lifecycle `create → claim → heartbeat →
finish`, with owner-bound leases and explicit terminal states. `reconcile`
allows an adapter to atomically release or mark stale claims after a process
or host failure. A rejected claim, heartbeat, finish, or checkpoint is a
compare-and-set conflict and must not be treated as successful work.

`ResumableWorkflowEngine` stores an opaque workflow state plus a monotonic
checkpoint sequence and next-step cursor. Resume claims the job and reads the
last durable checkpoint. Checkpoints cannot move backwards and use an expected
sequence, so two workers cannot silently overwrite progress.

## Scope

This slice adds only the application jobs port, the application capability
services, focused contract tests, and this evidence document. It does not
modify existing stores, migrations, transports, or the implementation spec.
Adapters implement `JobRegistry` and `WorkflowRepository` when a durable
backend is selected.

## Evidence

- `tests/test_wp3_seam010_jobs.py` covers durable identity validation,
  owner-bound claim/heartbeat/finish, reconciliation, terminal-state rules,
  workflow resume, monotonic checkpoints, and checkpoint conflicts.
- Focused gate: `python -m pytest tests/test_wp3_seam010_jobs.py`.
- No commit, push, specification checkbox, or evidence-status edit is part of
  this slice.
