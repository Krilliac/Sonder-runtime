# Agent, Autopilot & Fleet

Three layers of increasing autonomy, all built on a guarded tool loop.

## The agent tool loop (`workbench_agent`)

A Claude-style local loop: the model chooses one JSON tool call at a time,
receives the observation, and continues until it returns
`{"final": "..."}` or hits `max_steps`. It enforces:

- a **guaranteed checklist** (inspect → implement → validate → report).
  If the run stops early, the failing step is `blocked`, the report step is
  `done`, and any step it left open is closed as `canceled`, so `/tasks`
  never shows an abandoned attempt as live work;
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
- Each invocation is bounded by `max_cycles` tasks and a wall-clock budget
  (`SONDER_AUTOPILOT_MAX_WALL_SECONDS`, default 3600s). Either one pauses
  the run for an explicit resume.
- A cancelled run closes its open tasks as `cancelled` and keeps
  `passed`/`failed`/`uncertain` tasks as evidence.

Control: `/autopilot status|resume|cancel`, or the master orchestrator
tools. See [autopilot-interruption](../runbooks/autopilot-interruption.md).

Writing runs without an explicit project (`default`, empty, or an unresolved
project name) use `<state-home>/creations/<run-id>/`. Autopilot persists this
folder in its project field, shown by the app and by `working in:` in start,
status, and report text. Standalone writing agents also allocate a creations
folder before opening their lanes. Inside the agent loop only a named project
that resolves to no directory is upgraded; an omitted project stays unbound for
host-owned callers that keep their own root (the selfmod editor's candidate
workspace, the web research agent, unsafe lab). Existing project directories keep their
selected scope; read-only observe runs keep their existing behavior. A default
state home inside a Sonder Git checkout is refused instead of writing artifacts
into the Runtime source. Configured workspace grants still apply to delegation.

When unattended execution verifiers are refused (for example in `acceptEdits`),
an implementation may pass using successful read-back of every changed file
followed by host-owned, non-executing syntax checks: Python AST/compile, JSON,
TOML, HTML tag structure, and XML/SVG parsing. Self-contained HTML/SVG/XML requests
also reject external HTTP(S) `src`/`href` dependencies. Receipts retain paths,
read-back tool names, checker identities, and file digests; later writes invalidate
the evidence. These checks establish static structure, not runtime behavior.

Unsupported artifacts with successful write/read-back evidence are marked
`passed_unverified`: **written, not executed: needs `<command>` to verify**.
Execution-dependent validation tasks stay pending with the required approval
and command visible. Runnable report tasks still run, then Autopilot pauses for
approval without consuming a failure/retry budget or claiming completion.
Explicit resume retries pending validation. Modes that permit an execution
verifier (including an explicit allow rule) still require the real verifier.
Malformed files, missing or stale read-back, failed mutations, and unknown
mutation paths remain failures; no permission or path-confinement gate changes.

Steering (`/autopilot steer|clarify <id> <message>`) is owner-scoped and
fails closed for unowned runs. Runs started from the console
(`/autopilot plan|run`, `/mission start`) carry an opaque console owner
(`rc-<digest of OS user and state home>`, stable across console restarts),
so the console can steer them. Status, pause, resume, and cancel from the
console stay unscoped and still reach every local run. Runs started before
this change are unowned and cannot be steered; cancel and restart them if
they need steering.

## Fleet

Parallel worker execution (`fleet.db`, `fleet_store.py`) for fan-out work.
For `fleet`, `swarm`, and `fanout`, an omitted or nonpositive agent count
(including `0`) queues `min(max_agents(), max(3, 2 * worker_slots))` agents:
three at one available slot, eight at four slots, bounded by the configured
agent ceiling. Explicit positive agent counts retain the existing clamp;
`worker_cap` and the `use N workers` directive retain their per-run behavior.
The ordinary `delegate` mode still defaults to three agents.

The master strips one leading routing prefix before creating task digests or
delegating: optional `/master` or `master`, then `fleet`/`swarm`/`fanout`, then
an optional bare integer. For example, `fleet 0 make me something cool`
delegates `make me something cool`; `master swarm 6 compare ideas` requests
six agents. A positive API `agents` argument takes precedence over the prefix
count. Words and numbers inside the remaining sentence are unchanged.

Ordinary multi-agent briefs start with the exact authoritative task and then
include a deterministic `Angle k/N` suggestion (deliverable plus audience,
constraint, or technique). The task remains authoritative. Single-worker
briefs and protected `[objective:...]` contracts retain their original bytes
and do not receive angles. Angles encourage diversity; they do not guarantee
different model outputs or grant tools.

The start response shows queued agents, worker slots, and the stripped task.
When agents outnumber slots, the estimated worker time is
`agents / worker_slots * 30 seconds`, explicitly labelled as a default estimate;
audit time is extra and later resource pressure can extend it.

Delegated/fleet requests with explicit creation or implementation intent
(`make me something cool`, `build an app`, `write a script`) use build workers.
Without a project, the host allocates fresh folders under
`<state-home>/creations/<master-id>/worker-01/`, `worker-02/`, and so on.
Each worker uses the existing project-bound agent loop rooted at its own
folder, with file read/write/edit tools and the bounded execution tools that
already have project-scope contracts (`workspace_run` and `script_run` on this
version). Tools without such contracts are not added. Normal permission modes,
approval gates, inspection-before-mutation, and validation checks still apply;
plan mode refuses writes. Build fleets refuse unsafe-lab mode. Tool availability
does not authorize a mutation or execution, or add web/hosted-model access.

The start response names the output workspace. The aggregate lists every worker
folder, host-observed files, and check results, including missing receipts. Its
deterministic candidate ranking prefers passing checks, then untested candidates,
then failed/unverified checks; file count and worker order break ties. Empty
folders are never recommended. The audit explains the recommendation and risks.
Existing project-bound behavior is unchanged when `project` is supplied.

Questions, reviews, designs, comparisons, quoted commands, and uncertain intent
stay advisory. Those greenfield workers return proposals with no filesystem or
shell tools; the plan points to `/autopilot` for building within a project.
Inline grounded creative-build routes retain their existing behavior.

Master worker/audit synthesis and multi-model ensemble synthesis preserve
Ollama output budgets. Bridged `sonder_inference` calls reserve 2,048 additional
tokens for hidden thinking, capped at 16,384 total (the master's historical
1,400 becomes 3,448). Provider `done_reason`/`finish_reason` values of `length`
append `(summary truncated at the output limit)`. The local-only durable fanout
synthesis keeps its budget and also discloses a length stop. This is a bounded
budget, not a guarantee that every model will finish its summary.

The default worker width remains hardware-derived (CPU, available RAM, VRAM,
and Ollama batch width). AI harness/research/data runs can opt into a wider
single run with `master_orchestrate(..., agents=24, worker_cap=24)` or the clear
task phrase `use 24 workers`. The override is shown in `master_status` and
`master_capacity`, ends with that run, and is clamped to the operator ceiling.
`SONDER_MAX_WORKER_CAP` may lower that ceiling; the compiled absolute ceiling is
64, so malformed or enormous values cannot create unbounded threads.
The phrase must start the request (`use|run|spawn|launch N workers|agents`),
and it is ignored when the request also contains a negation (`not`, `no`,
`never`, `don't`), an explanatory or quoting word (`ignore`, `quote`,
`phrase`, `document`, `instruction`, `example`, `say(s)`, `mention(s)`,
`explain`, `why`), a comparative (`more/fewer/less than`), quotation
marks or backticks, or a second worker count. This keeps a quoted or
discussed count from starting a fleet. When a cue is ignored, the route
header (and the console) prints a `note:` naming the word that disabled
it; use `/master fleet <task>` to fan out explicitly.
Statuses `queued → running → done | failed | cancelled | interrupted`, with
`interrupted`/`failed`/`cancelled` re-dispatchable to `queued`. Claims use
compare-and-set; heartbeats detect stale owners. Two model instances (e.g.
two `facts.` sticks) roughly double fleet throughput.

A lane that makes no progress within the progress deadline is declared
stalled and the fleet result becomes uncertain. The default deadline is
`max(120, SONDER_TIMEOUT + 60)` seconds (360 with the default 300-second
model timeout). An explicit `SONDER_FLEET_PROGRESS_DEADLINE_SECONDS` is
honored as given. A lane inside a model call does not update its row until
the call returns, so it is never declared stalled before that call's own
timeout plus 60 seconds, whatever shorter deadline is configured. The old
fixed 120-second default was shorter than the model timeout, so slow CPU
hosts discarded valid late results.

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
