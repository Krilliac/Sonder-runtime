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

When a run started on the default (`auto`) tier ends because its model could
not drive the loop (a transport failure, or no parseable decision after the
format repairs), the runtime reruns the task on the next distinct bound local
model of the capability ladder, at most twice, and prefixes the output with a
`model escalation:` line naming each step. The same happens when a run
claims completion without changing anything or running any validation and
the request asked for a change or a check (by its action verbs; a read or
an explanation never triggers it). Any other finished run stands, and an
explicit tier never moves; see the automatic escalation section of
[Tiers & Gateway](08-model-tiers-and-gateway.md).

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

Research tasks can opt into deterministic provenance checks with bounded,
standalone marker lines:

```
[objective:history-eval|file:eval_history.py|symbol:main]
```

For such tasks, the master and delegated-task SHA-256 digests and objective IDs
are immutable fleet-row and event fields. The delegated prompt labels the master
task and objective contract as authoritative; retrieved lessons and tool output
remain non-authoritative context. Exact host-observed file/symbol evidence is
required before a worker result is accepted and again before aggregation. Marker
text embedded in prose, quotations, or fenced code is rejected as ambiguous;
private/control-plane paths and reparse targets are never valid objectives. The
host verifies the exact target and symbol through a stable bounded file handle,
then rejects a result if that target changes during the model call. Protected
objective runs require local worker and audit tiers, including retries, so task
text and repository evidence cannot be routed to a hosted model. Missing or
displaced coverage produces `task_drift`, suppresses the drifted output, and does
not enter the learning path. Inline protected runs use the same checks. Runs
without objective markers retain the ordinary fleet behavior.

### Model fanout

Model fanout asks each eligible chat model the same bounded question and records
a durable receipt. In the REPL, use an imperative whole turn such as:

```
ask all available local models: summarize this design
ask all local and cloud models: compare these alternatives
ask all loaded local chat models: review this patch plan
```

`local`, `cloud`, and combined requests select only discovered chat-capable
models. The `loaded local chat` form is deliberately no-load: it fails closed
unless Ollama reports every selected local target as already resident. Cloud
fanout additionally requires the operator's cloud opt-in; on a shared deployment
it requires developer authorization. Local models run serially to avoid VRAM
contention. Cloud work is bounded to two concurrent calls by default and failed
cloud calls are not automatically retried.

The result reports selected, answered, failed, unknown, skipped, and elapsed
counts. Use `/fanouts` to recover safe recent summaries after restarting the
REPL. Full receipts can include model answers, so `model_fanout_status` is
owner-scoped and developer-gated on shared deployments; local-open use keeps the
full local toolset.

Durable fanout receipts are recovery evidence, not an archive: terminal runs,
their events, and idle model-health rows expire automatically after seven days.
`SONDER_FANOUT_TTL_SECONDS` overrides the retention (clamped between one hour
and one year; `0` disables automatic expiry). Active runs, live worker leases,
and model-health rows with an active cooldown are never expired.

## Idempotency & recovery

Autopilot/fleet control requests carry durable operation IDs; retrying a
control request with the same idempotency key returns the existing
operation instead of starting a duplicate. On unclean shutdown, the drain
sequence marks unfinished ownership interrupted so recovery is deliberate.

## Private compute fabric

Fleet parallelism and compute placement are related but distinct. Fleet splits
model/agent tasks into workers; the compute fabric places one cataloged build,
test, index, analysis, fuzz, embedding, training, render, encode, service,
container, or storage job on one measured host.

Remote compute requires both the operator's `[compute].allow_remote=true` gate
and per-workload `allow_remote=true`. Local fallback is a separate request flag.
The scheduler uses fresh authenticated snapshots and cannot widen configured
node, workload, capability, or workspace authority. Programs and fixed
arguments live in the worker's catalog; a controller cannot submit an arbitrary
executable. Inference remains in the model gateway/Ollama pool.

See [Private Compute Fabric](../runbooks/compute-fabric.md) for configuration,
networking, catalog examples, ambiguity reconciliation, and recovery.

## Speculation interplay

While the model generates its next tool decision, the host can
speculatively run a predicted **read-only** tool call and retire it if the
model commits to the same call — hiding tool latency inside model time.
See [Speculation & Prediction](11-speculation-and-prediction.md).
