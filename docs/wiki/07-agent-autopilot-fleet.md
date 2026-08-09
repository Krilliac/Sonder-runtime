# Agent, Autopilot & Fleet

Three layers of increasing autonomy, all built on a guarded tool loop.

## The agent tool loop (`workbench_agent`)

A Claude-style local loop: the model chooses one JSON tool call at a time,
receives the observation, and continues until it returns
`{"final": "..."}` or hits `max_steps`. It enforces:

- a **guaranteed checklist** (inspect → implement → validate → report);
- **inspect-before-mutate** (no file change before workspace evidence);
- **validate-after-mutate** (a grounded check must pass before final);
- **negative-claim review** ("there are no X files" triggers a re-check);
- **no-progress guards** (a call that failed repeatedly is not re-run);
- a **host receipt** with the exact action transcript and project scope.

Tool decisions are parsed tolerantly: `_extract_agent_json` strips
markdown fences and uses a string-aware balanced-brace scan, so a model
that wraps or over-explains its decision still drives the loop
(genuinely truncated JSON still triggers a re-prompt).

Model size matters here. In live runs a 1.5B model broke the JSON protocol
mid-run; a 7B completed it cleanly 4/4. The loop is the same; the model's
ability to drive it scales with size — see [Tiers & Gateway](08-model-tiers-and-gateway.md).

## Autopilot

Durable, restart-safe autonomous goal runs (`autopilot.db`,
`autopilot_store.py`). A run has an objective, a plan, a checklist, and an
**owner** with heartbeats. Lifecycle states:

```
ready/planning → running → paused | blocked | completed | failed | interrupted | cancelled
paused → running | cancelled
interrupted → running   (explicit resume only)
failed → running        (explicit retry only)
completed / cancelled   (terminal)
```

The pure state machine lives in
`sonder_runtime/domain/automation/state_machine.py`; the store's
compare-and-set SQL is the concurrency authority. Invariants:

- Interrupted work stays **explicit** and is never silently replayed.
- A dead owner's work transitions to interrupted (process-liveness probe);
  unknown liveness never causes two owners (no split-brain).
- Terminal tasks do not replay. Budgets hold even when planners/models fail.

Control: `/autopilot status|resume|cancel`, or the master orchestrator
tools. See [autopilot-interruption](../runbooks/autopilot-interruption.md).

## Fleet

Parallel worker execution (`fleet.db`, `fleet_store.py`) for fan-out work.
The default worker width remains hardware-derived (CPU, available RAM, VRAM,
and Ollama batch width). AI harness/research/data runs can opt into a wider
single run with `master_orchestrate(..., agents=24, worker_cap=24)` or the clear
task phrase `use 24 workers`. The override is shown in `master_status` and
`master_capacity`, ends with that run, and is clamped to the operator ceiling.
`SONDER_MAX_WORKER_CAP` may lower that ceiling; the compiled absolute ceiling is
64, so malformed or enormous values cannot create unbounded threads.
Statuses `queued → running → done | failed | cancelled | interrupted`, with
`interrupted`/`failed`/`cancelled` re-dispatchable to `queued`. Claims use
compare-and-set; heartbeats detect stale owners. Two model instances (e.g.
two `facts.` sticks) roughly double fleet throughput.

## Idempotency & recovery

Autopilot/fleet control requests carry durable operation IDs; retrying a
control request with the same idempotency key returns the existing
operation instead of starting a duplicate. On unclean shutdown, the drain
sequence marks unfinished ownership interrupted so recovery is deliberate.

## Speculation interplay

While the model generates its next tool decision, the host can
speculatively run a predicted **read-only** tool call and retire it if the
model commits to the same call — hiding tool latency inside model time.
See [Speculation & Prediction](11-speculation-and-prediction.md).
