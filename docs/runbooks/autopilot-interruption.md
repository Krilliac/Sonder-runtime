# Autopilot / fleet interruption

Durable work is owned: every run/task records its owner process and
heartbeats. A crash, drain deadline, or kill leaves work in an explicit
`interrupted` state — it is never silently replayed.

## After an unclean stop

1. Check state: `GET /v1/sonder/status` (autopilot + agents sections), or
   the REPL `/autopilot status`.
2. Interrupted runs show `[interrupted/interrupted]` in the ledger.
   `/autopilot status <id>` lists the first 12 plan tasks with their status:
   passed tasks are done, and the task that was in flight still shows
   `[running]`. The full plan is in `autopilot.runs[].plan` of
   `GET /v1/sonder/status`. Review what the run
   was doing before resuming — interrupted mid-write workspace changes
   should be inspected directly in the workspace first (`git status` /
   `git diff`); `/activity` and the execution feed are held in process memory
   and do not survive the stop.
3. Resume explicitly (`/autopilot resume <id>`) or cancel
   (`/autopilot cancel <id>`). Resume re-claims ownership with a fresh
   heartbeat; a stale owner cannot overwrite the new one. A task that was
   running at the interruption is never replayed: resume marks it
   `uncertain` and pauses the run again for operator review. An `uncertain`
   task never counts as passed, so that run cannot complete; cancel it and
   start a fresh scoped run.

Cancelling a run closes its pending and running tasks (a task waiting on a
provider retry keeps `infra_retry`): the task that was running shows
`[cancelled]` with a note that its result was discarded and any tool
effects it already made were not rolled back. Pending tasks show
`[cancelled]` too. Passed, failed, and `uncertain` tasks keep their
status as evidence.

## Paused on a budget

Each invocation (a start or a resume) has two budgets: `max_cycles`
tasks and a wall-clock budget (`SONDER_AUTOPILOT_MAX_WALL_SECONDS`,
default 3600 seconds, at most 24 hours). The wall-clock budget is checked
at every host checkpoint before the next task starts, so the task already
running may finish past it. Reaching either budget pauses the run with the
reason in its summary. Review the progress, then `/autopilot resume <id>`
to continue with fresh budgets.

## Provider retries and verifier availability

`SONDER_AUTOPILOT_INFRA_RETRIES` defaults to `3` (clamped to 0..3;
invalid values use 3). Classified provider outages during planning and task execution (reviewer
outages fail the run) retry after 30, 60,
and 120 seconds without consuming task attempts, cycles, or failures.
Exhaustion pauses the run with `provider unavailable`; a task's retry count
survives an explicit resume.

`SONDER_AUTOPILOT_REQUIRE_VERIFIER` defaults to `0`. Verifier preflight
is advisory: start and status show `verifiers: allowed [...] refused [...]`
and, when none are allowed, a warning that validate tasks cannot pass
unattended in the current permission mode. This preserves starting runs
in the default `manual` mode. Set this variable to exactly `1` to refuse
direct starts (`/autopilot run|start|plan` without `--goal`,
`autopilot_start`) before run creation when no verifier is allowed unattended;
goal- and mission-bound starts skip this preflight.
Other values keep the advisory behavior. Preflight does not change
permission rules, spend approvals, or relax validation evidence gates.

## Stuck "running" with a dead owner

Ownership uses process-liveness probes; a dead owner's work transitions
to interrupted automatically when staleness is detected. If a task shows
running with a dead PID for more than a few minutes:

1. Confirm the PID is gone: `ps -p <pid>`
2. Restart the service (drain-aware): `systemctl restart sonder`
3. Startup reconciliation marks an Autopilot run with an expired lease or a
   dead local owner interrupted; resume it (`/autopilot resume <id>`). Fleet
   work is marked interrupted on a later reconcile pass (a status read at least
   10 s after startup first sees the dead owner); retry it with
   `/agentretry <id>`.

## Invariants (violations are bugs, file them)

- Unknown liveness never causes two owners (no split-brain claims).
- Terminal (completed/cancelled) tasks never replay.
- Budget limits hold even when planners/models fail.
