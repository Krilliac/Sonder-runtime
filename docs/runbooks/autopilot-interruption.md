# Autopilot / fleet interruption

Durable work is owned: every run/task records its owner process and
heartbeats. A crash, drain deadline, or kill leaves work in an explicit
`interrupted` state — it is never silently replayed.

## After an unclean stop

1. Check state: `GET /v1/sonder/status` (autopilot + agents sections), or
   the REPL `/autopilot status`.
2. Interrupted runs list their last completed step. Review what the run
   was doing before resuming — interrupted mid-write workspace changes
   should be inspected first (`/activity`, execution evidence).
3. Resume explicitly (`/autopilot resume <id>`) or cancel
   (`/autopilot cancel <id>`). Resume re-claims ownership with a fresh
   heartbeat; a stale owner cannot overwrite the new one.

## Stuck "running" with a dead owner

Ownership uses process-liveness probes; a dead owner's work transitions
to interrupted automatically when staleness is detected. If a task shows
running with a dead PID for more than a few minutes:

1. Confirm the PID is gone: `ps -p <pid>`
2. Restart the service (drain-aware): `systemctl restart sonder`
3. The claim reaper marks the work interrupted on startup; resume it.

## Invariants (violations are bugs, file them)

- Unknown liveness never causes two owners (no split-brain claims).
- Terminal (completed/cancelled) tasks never replay.
- Budget limits hold even when planners/models fail.
