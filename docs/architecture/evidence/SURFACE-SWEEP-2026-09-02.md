# Surface sweep of every command, surface and routing path — 2026-09-02

`scripts/surface_sweep.py` drives every catalogued command (310: 104 native
slash commands and 208 tool-backed ones, including the 16 typed file tools)
on every surface Sonder Runtime has, plus the natural-language router and the
CLI entry point, in a hermetic home and workspace with the model stubbed, and
classifies what each caller sees. It is a sweep, not a test: the point is to
put one build in front of every door at once and read what comes back. It is
rerunnable (`--out`, `--mode`, `--surfaces`, `--only`, `--live-model`) and
was run twice for this record: unattended in the default `manual` mode and in
`auto`.

## Surfaces exercised

| surface | what the sweep drove |
|---|---|
| `control` | `server.control_command` with every command as a slash line (the chain the app and chat share) |
| `console` | the REPL loop itself (`sonder_repl.main`), every command fed as a typed line, unattended (piped) |
| `mcp` | the legacy MCP server: `server.mcp.call_tool` for all 208 tools |
| `native` | the native MCP server over JSON-RPC (`run_native_mcp`), all 50 tools with their schema's required arguments |
| `http` | the served API in a thread: 15 `GET` routes and every command posted to `/v1/chat/completions` |
| `agent` / `agent-ro` | `server._agent_dispatch` for the 143 dispatchable tools, project-bound, as a mutable and as a read-only run |
| `router` | every phrase the two natural-language guides document, checked against the command they say it runs, plus the guides' "must not fire" phrases |
| `router-names` | every command's own name spoken plainly (`autopilot cancel`, `secret scan`), which must resolve to that command, an equivalent, or nothing |
| `cli` | `python -m sonder_runtime --version`, every subcommand's help, and the read-only subcommands against the sweep home |

Outcome classes: `ok` (answered), `gated` (the permission gate refused an
unattended caller as the mode says it should), `usage` (usage text or an
argument-shape error), `argument` (the command refused the synthetic argument
on its merits: no such id, not JSON, not a git repository), `containment`
(the guarded primitives refused a path or root), `model` (a model turn the
environment cannot make), `unavailable` (off by configuration: web tools,
process inspection, an unreachable service), `error` (an unclassified
`ERROR:` answer), `crash` (an exception escaped the surface), `timeout`,
`skipped` (a multi-line argument the line surfaces cannot carry).

## Results, `manual` mode (1882 invocations)

| surface | ok | gated | usage | argument | containment | model | unavailable | skipped |
|---|---|---|---|---|---|---|---|---|
| `agent` | 61 | 56 | 2 | 18 | 0 | 2 | 4 | 0 |
| `agent-ro` | 50 | 82 | 2 | 5 | 0 | 1 | 3 | 0 |
| `cli` | 22 | 0 | 1 | 0 | 0 | 2 | 0 | 0 |
| `console` | 163 | 98 | 10 | 31 | 0 | 2 | 5 | 1 |
| `control` | 170 | 98 | 5 | 30 | 0 | 2 | 4 | 1 |
| `http` | 182 | 98 | 6 | 32 | 0 | 2 | 4 | 1 |
| `mcp` | 102 | 71 | 2 | 27 | 0 | 2 | 4 | 0 |
| `native` | 21 | 13 | 0 | 9 | 0 | 1 | 6 | 0 |
| `router` | 58 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `router-names` | 310 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |

## Results, `auto` mode (1489 invocations, tool surfaces)

| surface | ok | gated | usage | argument | containment | model | unavailable | skipped |
|---|---|---|---|---|---|---|---|---|
| `agent` | 86 | 13 | 7 | 30 | 1 | 2 | 4 | 0 |
| `agent-ro` | 50 | 82 | 2 | 5 | 0 | 1 | 3 | 0 |
| `console` | 199 | 32 | 24 | 45 | 2 | 2 | 5 | 1 |
| `control` | 213 | 32 | 14 | 41 | 2 | 2 | 5 | 1 |
| `http` | 219 | 32 | 17 | 46 | 2 | 2 | 6 | 1 |
| `mcp` | 132 | 21 | 6 | 40 | 2 | 2 | 5 | 0 |
| `native` | 22 | 1 | 2 | 18 | 0 | 1 | 6 | 0 |

After the fixes below, no invocation on any surface, in either mode, ends in
`error`, `crash` or `timeout`. Every `gated` row is the unattended refusal the
mode promises (file changes, host programs and destructive tools in `manual`;
destructive tools and the shipped `file_delete` deny in `auto`); `auto` turns
`gated` rows into `ok`, `argument` and `containment` rows, which is the mode
doing what its blurb says.

## What the sweep found

Five defects, none of which a unit test had asked about, each fixed and
pinned in `tests/test_surface_sweep_findings.py`:

1. **`model_fanout` let a transport error out as a traceback.** With no
   reachable model endpoint the tool raised `urllib.error.URLError` through
   the control chain and the legacy MCP server. It now renders the same
   policy answer every other model tool renders.
2. **Native inspections crashed on omitted bounds.** A native MCP client that
   sent only its schema's required arguments got `KeyError: 'max_entries'`
   (`archive_list`, `workspace_compare`), `'max_bytes'` (`data_inspect`,
   `file_digest`), `'sql'` (`data_query`), `'tail_lines'` (`log_inspect`) or
   `'path'` (`dependency_inventory`, `directory_digest`, `project_detect`).
   The executor now fills the same defaults the legacy handlers take from
   their signatures (`inspection_defaults`, pinned against those signatures).
3. **Nine native tools were routed to a service that could not run them.**
   The native catalog groups `web_fetch`, `web_search`, `weather_lookup`,
   `approximate_location_lookup`, `process_list`,
   `process_memory_risk_inspect`, `artifact_risk_inspect`, `fetch_artifact`
   and `verify_artifact` with the inspections, and the route followed the
   group: every one answered "unsupported read-only inspection". The route
   now follows the one declared set the inspection adapter supports
   (`SUPPORTED_INSPECTIONS`); the rest reach the packaged executor.
4. **`run_program` and `run_script` had unbounded native schemas**, so an
   empty call reached the primitive and came back as a `TypeError`; both are
   bounded now. `json_patch` and `file_batch_write` name a missing
   operations list instead of raising the same `TypeError`.
5. **The router preferred a permuted command name.** "ground artifact"
   resolved to `/artifact_ground`, a different tool whose name is the same
   two words in the other order, because summary words broke the tie. A full
   name match now ranks the name spoken in its own order first.

And one thing the sweep did to itself, found when the first record was
being committed: its probes had appended "sweep probe" to the checkout's own
`sonder_runtime/platform/system_profile.md` twenty-five times, rewritten the
tracked `emotion_vectors.json`, and left generated asset packs and games
under the repository root. The harness had redirected the guarded file root
and the runtime home, but seven modules anchor a writable file to their own
directory (`system_profile`, `emotion_vectors`, `workflow_store`, `assetgen`
with `game_forge`, `self_heal`, `code_runner`) and refuse any path outside it,
so no environment variable could move them; the generators build their
output path from the working directory. The sweep now redirects every such
root into its home, runs from there, and compares the checkout before and
after the run: any path it changed is a harness finding and fails the run.
`tests/test_surface_sweep_findings.py` pins both the guard and the list of
roots against the tree (a new module-local root fails the test). The numbers
above are from the reruns after that fix; the checkout was unchanged in both.

Two things the sweep turned up are recorded rather than changed:

- `process_list` and `process_memory_risk_inspect` answer `opt_in_required`
  (`SONDER_PROCESS_INSPECTION`) on the native surface, which is the consent
  gate working; the legacy surface refuses them for the same reason.
- The console's `/read` with no path is refused by the typed schema
  ("shorter than minLength") rather than by a usage line; the answer is
  correct and the wording is the schema's.

## Natural language

All 58 phrases the two guides document (`docs/NATURAL_LANGUAGE_TOOLS.md`,
`docs/NATURAL_LANGUAGE_CAPABILITY_QUERIES.md`) resolve to the command the
guide names, and every "must not fire" phrase falls through. All 310 command
names spoken plainly resolve to the command itself, to an equivalent native
slash (`autopilot cancel` to `/autopilot cancel`, `master status` to
`/agents`), or to nothing; none resolves to a different command.

## What the sweep cannot see

No local model runs in the sweep's environment, so every model-backed turn
is answered by a stub that finalises immediately; `--live-model` uses the
configured endpoint instead. The Flutter app is exercised through the served
API it talks to, not itself. Web, weather and location tools are off by
configuration and answer accordingly.

## Rerun

```sh
python scripts/surface_sweep.py --out eval_runs/sweep --mode manual
python scripts/surface_sweep.py --out eval_runs/sweep --mode auto --surfaces control,console,mcp,native,http,agent
```

Each run writes `sweep-<mode>.json` (every record) and `sweep-<mode>.md`
(the tables above and a "needs reading" list of every `error`, `crash` and
`timeout`), and exits non-zero if the checkout differs after the run from
before it, naming the paths.
