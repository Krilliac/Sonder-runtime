# Natural-Language Capability Queries

Asking Sonder about *itself* — what tools it has, which model is loaded, how
well calibrated it is, how its learning is doing, whether the MCP registry has
converged, and what standing instructions it is carrying.

Every command in this guide is catalogued **`safe`**: it reads runtime state
and returns text. Nothing here writes a file, runs a command, starts a
workflow, or reaches the network.

## Where each form works

| Surface | Form | Where it works |
|---|---|---|
| Natural phrase | `list your tools`, `mcp status` | **Console REPL only** |
| Slash command | `/tool_manifest`, `/status` | Console REPL; HTTP chat (role-gated subset) |
| Direct tool call | MCP tool `tool_manifest` (`mcp__sonder__tool_manifest` from an MCP client) | Any MCP client; the agent loop |

`command_router` — the module that maps a phrase to a slash line — is imported
by `sonder_repl` and by nothing else. The HTTP API and MCP clients therefore
never see natural phrasing; they take the exact contracts, which is what makes
those contracts scriptable.

Natural phrasing is a **console convenience, never a capability widening**. A
recognized phrase is rewritten to a slash line, echoed back as
`(interpreted as: /tool_manifest)`, and then dispatched exactly as if you had
typed the slash command — same permission mode, same per-tool rules, same
refusals.

## Basic — one phrase, one read-only tool

Each line below is a complete turn. Type it on its own and it dispatches.

| You type | Runs | Answers |
|---|---|---|
| `what tools do you have?` · `list your tools` · `show me your tools` | `/tool_manifest` | the MCP tool list and what each one is for |
| `what model are you running?` · `which models are loaded` · `model status` | `/status` | local-model state and current VRAM residency |
| `how reliable are you?` · `how well calibrated are you` · `show your calibration` | `/calibration_status` | measured reliability, split by population |
| `learning health` · `how is your learning going?` · `show your learning health` | `/learning_health_status` | outcome coverage, positive signals, lesson provenance |
| `mcp status` · `show me the mcp runtime status` | `/mcp_runtime_status` | live tool-registry convergence and fail-closed refresh state |
| `show your standing instructions` · `what is your system profile` | `/system_profile_text` | the editable standing instructions injected into Sonder |

Related surfaces that already had phrasings, unchanged by this guide:
`what tools are installed` → `/env` (host toolchains, not Sonder's tools),
`what tools ran` → `/activity` (recent tool calls), `show me your stats` →
`/stats`, `context health` → `/context`, `show permissions` →
`/permissions`, `what can you do` → `/help`.

## Intermediate — what does *not* fire, and why

A phrase resolves only when the pattern consumes the **entire turn**
(`command_router.resolve` uses `fullmatch`). Everything in this table falls
through to ordinary chat/work handling and dispatches nothing:

| You type | Why it falls through |
|---|---|
| `what tools do you have for editing images and can you use one on logo.png` | carries a follow-up action; converting it to a command would silently drop the rest |
| `the guide says list your tools but nothing happens` | quoted / retrieved prose must never dispatch |
| `is "list your tools" supposed to work?` | same, in explicit quotes |
| `how do I list your tools` | a question *about* the command, not the command |
| `show me your calibration data for the last week` | the trailing scope is not something `/calibration_status` takes |
| `explain how mcp status works` | asks for an explanation, not a status read |
| `update your standing instructions to prefer tabs` | reading is `/system_profile_text`; *writing* is `/update_system_profile`, which is never reached from a phrase here |

That last row is the shape of the whole boundary: the read tool gets natural
phrasings, its mutating neighbour does not. `/update_system_profile` and
`/runtime_policy_update` stay reachable only by naming them, under the
catalog's adjacency rule for risky commands.

When a phrase does not fire, the exact form always still works:

```text
sonder > /tool_manifest
sonder > /calibration_status
sonder > /system_profile_text
```

## Advanced — resolution is not authorization

Resolving a phrase decides *which* command runs, never *whether* it may. The
synthesized slash line re-enters the console at the same choke point as a
typed one and passes `permission_modes.decide_for_caller` before the tool's
handler is called. A `deny` rule refuses it:

```text
sonder > list your tools
(interpreted as: /tool_manifest)
refused /tool_manifest: denied by rule (mode: manual)
```

Practical consequences:

- **A phrase can never reach a tool the slash command could not.** If
  `/tool_manifest` is denied, `list your tools` is denied identically.
- **`plan` mode still allows these**, because they are `safe` reads — that is
  the point of the slice. Any command that writes keeps its ask/deny path.
- **Scripts should use the exact forms.** Phrase matching is intentionally
  conservative and can decline a turn (a tie or an unaccounted word resolves
  to nothing). `/status` and the MCP tool `status` are deterministic; a phrase
  is a convenience for a human at a console.

The same read from an MCP client, where no phrasing exists:

```jsonc
// MCP tool call — the exact contract, no natural phrasing involved
{"name": "learning_health_status", "arguments": {}}
```

## Multi-tool — sequences worth typing

Each turn is one command; the value is the order. Type them one per line.

**Session opener — "what am I talking to?"**

```text
what model are you running?      -> /status
list your tools                  -> /tool_manifest
show your standing instructions  -> /system_profile_text
show permissions                 -> /permissions
```

**Trust check — "should I believe this answer?"**

```text
how reliable are you?            -> /calibration_status
learning health                  -> /learning_health_status
what tools ran                   -> /activity
```

**Plumbing check — "are the tools actually wired up?"**

```text
mcp status                       -> /mcp_runtime_status
what tools are installed         -> /env
show hardware                    -> /hardware
```

Nothing chains automatically: there is no phrase that runs several tools in
one turn, by design. A turn that asks for two things resolves to nothing
rather than guessing which one you meant.

## Limitations

- **Console only.** No HTTP or MCP caller sees these phrasings.
- **Exact wording.** `model status` dispatches; `model status please` does
  not. The phrases are a curated list, not an intent classifier — a near miss
  costs you one retry or one slash command, and never dispatches something you
  did not ask for.
- **No arguments.** Every phrase in this guide maps to a zero-argument read.
  Anything that would need a target (a repo root, a tool name) is left to the
  slash form, where the argument is explicit.
- **The echo is the audit.** `(interpreted as: ...)` is printed before
  dispatch; if it names something you did not intend, nothing about the turn
  was hidden from you.

The regression tests for every claim on this page live in
`tests/test_natural_capability_queries.py`.
