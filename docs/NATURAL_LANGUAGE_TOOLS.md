# Natural-Language Tool Calling — A Progressive Guide

Sonder's tools are reachable three ways, and all three run the **same
implementation behind the same permission gate**:

| Surface | Form | Where it works |
|---|---|---|
| Natural phrase | `git status` / `show recent commits` | Console REPL only |
| Slash command | `/repo_status`, `/read notes.txt` | Console REPL; HTTP chat (`/v1/chat/completions`, role-gated subset) |
| Direct tool call | MCP tool `repo_status` (`mcp__sonder__repo_status` from an MCP client) | Any MCP client; the agent loop |

Natural phrasing is a **console convenience, never a capability widening**:
a recognized phrase is resolved to a slash line, echoed back as
`(interpreted as: /repo_status)`, and then dispatched exactly as if you had
typed the slash command — including the permission mode, per-tool rules,
approved file roots, and confirmation tokens. The slash and MCP forms remain
the exact, unambiguous contracts.

## How natural routing works — and what it refuses

- **Whole-turn anchored.** A phrase resolves only when the pattern consumes
  the *entire* message. `git status` dispatches; `git status and then push`,
  `the web page says git status`, and `"git status"` all fall through to
  ordinary chat/work handling. This is deliberate: quoted or retrieved text
  can never trigger a command, and a follow-up action is never silently
  dropped.
- **Hand-written rules first, then a conservative catalog match.** ~50
  curated patterns extract arguments (`read the file notes.txt` →
  `/read notes.txt`). If none match, the turn is compared against the whole
  command catalog under four gates: it must open imperatively or name a
  multi-word command outright, every name token must be present, every
  remaining word must be accounted for, and the winner must be unique. A tie
  or a leftover word resolves to *nothing*.
- **Risky commands must be named.** Commands graded `mutation` or
  `dangerous` are never inferred from summaries or loose matches — their name
  tokens must appear adjacent in the turn (`delete task abc` →
  `/task_delete abc`). A read verb aimed at a mutating command (`list the git
  branches` vs `/git_branch`, which *creates* one) resolves to nothing.
- **The permission gate still runs.** Every resolved command passes
  `permission_modes.decide()` before its handler is called. The four autonomy
  modes are `plan` (reads only), `manual` (ask before any non-read; default),
  `acceptEdits`, and `auto`. `dangerous` tools ask in every mode when an
  operator is present, and per-tool `deny` rules always win. No mode grants
  elevation; privilege is a separate, session-scoped act.

## Basic — one phrase, one read-only tool

These complete turns each map to a single safe, read-only command:

| You type | Runs | Shows |
|---|---|---|
| `git status` / `what's the git status?` / `is the working tree clean?` | `/repo_status` | working-tree status |
| `git log` / `show recent commits` | `/repo_log` | recent commit history |
| `git diff` / `show the diff` / `show unstaged changes` | `/repo_diff` | unstaged patch |
| `show uncommitted changes` / `show pending changes` | `/repo_status` | staged, unstaged, and untracked state |
| `health check` / `run diagnostics` / `are you healthy?` | `/diagnostics` | local install health |
| `show me your stats` | `/stats` | runtime statistics |
| `context health` | `/context` | context budget meters |
| `show agents` | `/agents` | orchestration status |
| `what tools ran` | `/activity` | recent tool activity |
| `show permissions` | `/permissions` | active policy |
| `what can you do` | `/help` | command help |

Catalog reach without a curated pattern: `scan for secrets` →
`/secret_scan`, `list processes` → `/process_list`, `what's my task
progress` → `/task_progress`, `show npu status` → `/npu_status`.

## Intermediate — phrases that carry an argument

| You type | Resolves to |
|---|---|
| `read the file notes.txt` | `/read notes.txt` |
| `find files matching *.md` | `/files *.md` |
| `remember that the venv is required` | `/fact the venv is required` |
| `switch project to duetos` | `/project duetos` |
| `weather in Berlin` | `/weather Berlin` |
| `what version is cargo?` | `/toolstatus cargo` |
| `switch to the reasoning tier` | `/model reasoning` |
| `create a new rust project named forge` | `/scaffold rust forge` |
| `run saved workflow status_sweep` | `/workflow_run status_sweep` |
| `get a second opinion on the lock ordering` | `/consult the lock ordering` |
| `which model should handle a lookup table` | `/route a lookup table` |
| `improve the parse function in foo/bar.py` | `/refactor foo/bar.py parse` |

The same anchoring applies: `read the file notes.txt and summarize it` is
*work*, not a command, and goes to the agent. Mutating forms keep their
guards — `delete the file scratch.txt` resolves to `/delete`, which is graded
`dangerous`, always asks, and dry-runs until you confirm with its token;
`/refactor` keeps its own confirmation step.

## Advanced — work requests and execution lanes

A concrete workspace request (an action verb plus a workspace target, a
path, or an explicit "fix it"/"use the tools") is classified as **work** and
runs through the guarded foreground workbench agent: real file inspection,
edits, and validation, bounded steps, each tool call individually
permission-gated. Explanatory questions ("how do I…", "explain…") never
classify as work.

Explicit phrasing selects a bounded execution lane:

- `plan only` / "make a plan but do not execute" → persistent plan-only run
  (nothing executes).
- "autonomously", "keep working until the tests pass", "end-to-end" →
  Autopilot (durable background run with its own status/pause/cancel
  controls).
- "use a fleet" / "parallel subagents" → fleet orchestration.
- "foreground", "one-shot", "do it now" → the workbench lane.
- `use an ensemble (code + reasoning) with compiler-feedback retries to
  <task>` → the one fixed multi-model build loop. The host pins the model
  set, retry count, project root, permission mode, and executable policy —
  the phrase selects the lane, never its parameters.

Deterministic refusals happen before any planner or model sees the turn:
"no tools"/"just answer" disables tool routing for that turn, and a request
to evade containment *and* contact anything outside the runtime receives a
fixed refusal.

## Multi-tool workflows

- **One request, many tools.** You ask for the outcome; the workbench or
  Autopilot agent chooses and chains the tools itself, with every individual
  call still passing the permission gate. `fix the failing parser test and
  run the suite` is one work request — not a command chain.
- **Saved workflows.** `/workflow_save` captures a named sequence;
  `list my saved workflows` → `/workflow_list`; `run saved workflow X` →
  `/workflow_run X`, which still passes the `workflow_run` permission gate
  before any saved action executes.
- **Orchestration.** `orchestrate fix the parser and add tests` →
  `/master …` fans the goal out to subtask agents; monitor with
  `show agents`, `agent capacity`, `/fanouts`; stop with `cancel all agents`.
- **Autopilot.** `/autopilot start <goal>` / `status` / `pause` / `resume` /
  `cancel` for durable, owner-scoped background runs.

**What natural language will *not* do.** There is no model-decides-any-tool
grammar: a phrase either resolves to exactly one catalogued command or falls
through. Commands never auto-chain — "do X and then Y" is handed to the
agent as a normal request, not split into dispatches. Nothing here grants
autonomous cross-tool sequences beyond the explicitly requested lanes above.

## Malformed lines fail loudly, and structurally

`parse_invocation` refuses a malformed `/tool key=value` line instead of
repairing it into something that was not typed. Three shapes are rejected,
each raising `command_catalog.InvocationError` — a `ValueError` subclass
carrying `command`, `problem`, and `details` so programmatic surfaces can act
on *what* failed without parsing message text:

| Line | Problem | Why silence was worse |
|---|---|---|
| `/file_read path=x limit=5` | `unknown-parameter` | dropping `limit` reads the whole file while looking bounded |
| `/file_read path=a path=b` | `conflicting-duplicate` | last-wins read `b` while the line showed `a` |
| `/file_delete path=x dry_run=nope` | `invalid-value` | the raw string `"nope"` is *truthy*, so a typo'd flag meant the opposite of what it said |

An identical repeated key (`path=a path=a`) still binds — a retry-pasted
duplicate states one intent. Positional words keep lenient coercion: they
carry no stated `key=type` intent, and free-text parameters legitimately
absorb arbitrary words.

## Why didn't that resolve? — `command_router.explain`

`command_router.explain(text)` (diagnostic seam; no dispatch) runs the same
pipeline as `resolve` and reports which stage claimed or refused the turn:
`{"input", "resolved", "source", "detail"}` where `source` is one of
`empty | slash | tier | structured | rule | catalog | none`, and `detail`
carries the evidence — the winning rule's pattern, the tied `candidates` of
an ambiguous turn (`show the update status` → `['/status', '/update']`), the
`leftover` words that proved a turn asked for more than a command does, or
the named command a `risky-not-named` / `read-verb-on-mutation` gate
refused. It is guaranteed to agree with `resolve` (both run one shared
pipeline) and exists for tests, tracing, and "why didn't that run" answers.

At the console, `/why` renders that report: bare `/why` explains your
previous plain-language turn, `/why <text>` explains any turn without
dispatching it.

## Direct forms — the exact contracts

- **Console slash:** `/commands` lists the surface; `/help <name>` shows one
  contract. Every registered tool is callable as `/<tool_name>` with
  `key=value` arguments (e.g. `/repo_log path=. revision=HEAD`).
- **MCP:** every tool is a first-class MCP tool under its bare name
  (`repo_status`, `file_read`, `workflow_run`, …); `tool_manifest` describes
  the surface programmatically.
- **HTTP:** slash lines inside a `/v1/chat/completions` user message reach a
  served subset of the same commands, with hosted role checks and
  developer/admin gating on top (dangerous names are refused outright for
  unauthorized principals); `/v1/commands/help` and `/v1/commands/complete`
  expose contract help. Structured API turns do not use natural or slash
  forms at all — see the [HTTP API reference](wiki/05-http-api-and-lifecycle.md).
- Natural phrasing itself is **console-only by design**; served and
  programmatic surfaces take the exact forms.

## Limitations, in one place

- Natural resolution is intentionally conservative: when in doubt, the turn
  is chat, not a command. `None` is always the safe answer.
- Whole-turn anchoring means trailing prose, quoted text, or a second action
  falls through — retype the slash form when you want the exact command.
- Mutating/dangerous commands are only reachable by naming them, and keep
  their confirmations regardless of how they were reached.
- File tools stay confined to approved roots with bounded sizes; deletes
  dry-run until confirmed.
- Cloud tiers require explicit consent; hosted deployments add role checks
  the console does not need.
- The natural-phrase surface never grows implicitly: each supported phrase is
  a reviewed, tested pattern (`command_router.py`, `intents.py`), not a
  model's guess.

See also: [Security model](wiki/09-security-model.md) ·
[Tools & languages](wiki/10-tools-and-languages.md) ·
[Agent, Autopilot & Fleet](wiki/07-agent-autopilot-fleet.md)
