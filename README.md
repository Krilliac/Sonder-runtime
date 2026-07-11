# trilobite

<!-- ci-artifact-badges:start -->
[![Latest CI artifacts 4 files](https://img.shields.io/badge/Latest%20CI%20artifacts-4%20files-2088FF?style=for-the-badge&logo=githubactions&logoColor=white)](https://github.com/Krilliac/Trilobite-model/actions/runs/28965607688)
[![Android download](https://img.shields.io/badge/Android-download-3DDC84?style=for-the-badge&logo=android&logoColor=white)](https://github.com/Krilliac/Trilobite-model/actions/runs/28965607688/artifacts/8178000122)
[![Linux download](https://img.shields.io/badge/Linux-download-FCC624?style=for-the-badge&logo=linux&logoColor=black)](https://github.com/Krilliac/Trilobite-model/actions/runs/28965607688/artifacts/8177919570)
[![Windows download](https://img.shields.io/badge/Windows-download-0078D4?style=for-the-badge&logo=windows&logoColor=white)](https://github.com/Krilliac/Trilobite-model/actions/runs/28965607688/artifacts/8177965622)
[![macOS download](https://img.shields.io/badge/macOS-download-000000?style=for-the-badge&logo=apple&logoColor=white)](https://github.com/Krilliac/Trilobite-model/actions/runs/28965607688/artifacts/8177924740)
<!-- ci-artifact-badges:end -->

**A private AI that's *yours*.** It runs entirely on your own machine, learns from your work over time, and no one can read it, rate-limit it, reset it, or take it away.

trilobite is **not** trying to be smarter than ChatGPT or Claude — a local model won't win that race, and this README won't pretend otherwise. It's built for a different thing those can't give you: **privacy, ownership, and personalization.** Nothing leaves your computer. It works offline. It adapts to *you*. And it's yours to keep.

---

## The honest pitch (what this is, and isn't)

**What it is**
- **Local & private.** The model runs on your CPU/GPU via [Ollama](https://ollama.com). No prompt, no file, no lesson ever leaves your machine. Works on a plane, on sensitive data, with no subscription.
- **Self-improving on *your* context.** Every coding interaction can be captured, scored by real outcomes (did it compile? pass tests?), and distilled into short "lessons" it retrieves next time. Over time it learns *your* patterns, not the internet's average.
- **Yours to own.** You keep the weights, the memory, and the lessons. Nothing is rented or deprecated out from under you.

**What it isn't**
- **Not a frontier model.** A local 7B is genuinely *weaker* than a hosted giant. If you just want the smartest possible answer with zero setup, use ChatGPT/Claude — honestly.
- **Not "for literally everyone" yet.** Today it needs a decent machine and some setup. See the [Roadmap](#roadmap-toward-for-everyone) for how that changes.

**Who it's for:** privacy-conscious people, offline/field use, tinkerers and the local-LLM crowd, and organizations that need a private assistant on their *own* data (a clinic, a firm, a studio) — anyone who values *owning* their AI over squeezing out the last IQ point.

> The moat isn't the weights — a generically fine-tuned model just re-derives what you'd get from Google, and the base model already knows it. The moat is **your** private, grounded data: your code, your bugs and fixes, your conventions. trilobite is built to accumulate exactly that.

---

## How it works

trilobite is a frozen local model (Qwen2.5-Coder) wrapped in a small, dependency-light Python **learning loop**:

```
your task ─► retrieve relevant lessons ─► augment prompt ─► local model ─► answer
                     ▲                                                        │
                     │                                                        ▼
        distill a lesson ◄── (if good) ── you record the outcome (compiled? tests passed?)
```

- **Retrieval** — hybrid lexical (SQLite FTS5) + semantic (local embeddings), with a **relevance threshold** so only genuinely on-topic lessons are injected (irrelevant lessons hurt, so it injects nothing when nothing fits).
- **Capture** — every learning call is logged locally to `memory.db`.
- **Grounding** — you (or your fleet) call `record_outcome` with a real signal; execution outcomes are the reward.
- **Reflection** — a good outcome distills a deduped one-line lesson future prompts can retrieve.

The loop is model-agnostic: point it at whatever LLM you have. The local student
(`code`) is memory-augmented; a stronger paid/cloud model can act as a **teacher** —
it answers *clean* (no local-lesson injection) and its grounded good outcomes are
distilled into lessons and fine-tuning data the local model retrieves later. Which
tiers learn is configurable (`TRILOBITE_LEARN_TIERS`, default local-only:
`fast,code,general`). The memory, capture, and distillation always stay local; only a
cloud-tier *prompt* leaves the machine, and only after `TRILOBITE_ALLOW_CLOUD=1`.
The `fast`, `code`, and `general` aliases are local-only. Their shared mappings
and each execution lane's preferred alias live in the hot-reloadable runtime
policy described below. Environment values seed the first policy file; cloud
aliases and cloud opt-in remain separate host-owned configuration.

### Memory beyond lessons

The learning loop above is *cross-task* memory. On top of it trilobite also has:

- **Conversation memory (ON by default).** Successive `trilobite` calls remember each
  other, so follow-ups have context. With no `session`, calls share the `default`
  thread; pass a distinct `session` id to isolate a conversation, or `session="none"`
  for a one-off single turn. Threads persist in `memory.db` across restarts and are
  auto-titled; older turns are rolled into a running summary so a thread never
  overflows the local context window. (The 7B is a ~32K-context model on a 6 GB GPU —
  memory is *carrying the right turns + summarizing the rest*, not a giant window.)
  List/resume threads with `trilobite_sessions()` / REPL `/sessions` / `/resume`.
- **Semantic recall.** Each call also surfaces the most similar *past good-outcome
  solutions* (vector search over prior interactions), not just distilled lessons.
- **Project facts.** `trilobite_remember_fact(text, project=…)` stores durable facts
  (toolchain, conventions, key paths) that are injected into every call for that
  project — a mini-brief the model carries itself. Scope a call with `project=…`.
- **Dynamic user preferences.** Trilobite now captures clear preference statements
  like "I prefer concise status updates" during normal chat, stores them with
  evidence counts, and injects active preferences into future local-student
  prompts. Use `/prefer`, `/prefer <text>`, or `/prefer forget <id-or-key>` to
  inspect, teach, or disable them explicitly.
- **Visible activity.** Each response tracks observable work: model calls, tool
  calls, active response count, file creates/edits/deletes, and line deltas. Tool
  results retain a bounded, secret-redacted action transcript (`Viewed Image`,
  `Ran Script`, `Edited File`, etc.), while `/work` maintains a persistent ordered
  checklist and emits an evidence-backed end report. Use `/activity`, `/report`,
  `/checklist`, or the app's Workbench Activity panel while work runs.
- **Inspectable learning quality.** `/learning` and `learning_health_status()`
  report distinct outcome coverage, positive/negative signal mix, lesson source
  provenance, grounded distillation yield, embedding coverage, duplicate rows,
  search-index drift, missing sources, and redacted privacy-flag counts. The
  Flutter System page renders the same metrics as live meters and source/signal
  chips instead of reducing self-improvement to one opaque score.

---

## Four ways to run it

1. **Local, in your terminal** — `trilobite` (like launching `claude`). Interactive REPL routed through the full loop, with `/work`, `/report`, `/checklist`, `/inventory`, `/tree`, `/search`, `/programs`, `/scripts`, `/image`, `/mkdir`, `/runprogram`, `/runscript`, `/trace`, `/strict`, `/run`, `/train`, `/master`, `/agents`, `/capacity`, `/agentcancel`, `/agentretry`, `/asset`, `/artifactcheck`, `/forge`, `/game`, `/gamefleet`, `/todo`, `/commands`, `/activity`, `/runtime`, `/mcp`, `/learning`, `/dump`, `/permissions`, `/compact`, `/debug`, `/pass`, `/fail`, `/stats`, `/context`, `/quality`, `/privacy`, `/embeddings`, `/emotion`, and `/improve`, plus conversation commands `/new`, `/sessions`, `/resume`, `/project`, `/fact`, `/facts`. Concrete natural-language workspace requests route to the same guarded workbench. Each REPL launch is its own remembered thread.
2. **Hosted on your own server + a thin client anywhere** — run `deploy_trilobite.sh --serve` on your box (systemd service, API key), then any machine runs the single-file `trilobite_client.py` pointed at it. The serve layer threads the chat UI's own conversation history.
3. **Integrated with Claude/Codex** — the MCP `local-llm` tools include `workbench_agent`, budgeted workspace inventory, guarded tree/range/text/script/program/image inspection, argv-only program/script execution, persistent checklists, exact activity reports, agent/master orchestration, universal artifact generation, grounded game generation, bounded code/project runners, workflows, web tools, self-healing, privacy-safe memory review, local embedding backfill, and the remaining learning/memory surfaces. `master_orchestrate` can delegate to parallel subagents and audit their outputs; artifact and game tools create persistent assets/projects and accept only grounded checks. Local tiers remain the default for private workspace code.
4. **Mobile & desktop app (GUI)** — a cross-platform [Flutter client](app/) that talks to a hosted `trilobite_serve.py`. One codebase → an **Android APK** and **Windows/Linux/macOS** desktop apps, built in CI with downloadable installers. See [app/README.md](app/README.md).

### Persistent autopilot

Concrete natural-language work requests now pass through a visible execution
decision before any model answer. A small task runs in the guarded foreground
workbench; explicit `fleet`, `swarm`, spawn-as-many, or parallel-agent language
uses the hardware-bounded fleet; explicit `keep working`, `end-to-end`,
`autonomously`, or background language starts persistent Autopilot. For an
ambiguous compound request, a small local model may choose only between foreground
and Autopilot and may select one bounded local alias (`fast`, `code`, or
`general`). The response reports its mode, tier/model, source, reason, and
confidence.
`foreground only`, `plan only`, and `no tools` are deterministic overrides. The
router runs only for local-open or developer/admin-authorized requests, never
enables cloud or location, and defers instead of starting a second concurrent
Autopilot run. Slash commands remain explicit overrides.

`/autopilot run <objective>` turns one outcome into a durable, model-planned task
ledger. A local planner selects measurable success criteria and ordered tasks;
the guarded workbench executes one task at a time; successful inspection and
research tasks trigger evidence-aware checkpoints where a local reviewer may
keep or replace the remaining pending plan. Deterministic host code alone
enforces tool/root allowlists, local-only tiers, cycle/task/failure/replan budgets,
mutation validation, pause/cancel, and the completion gate. Superseded tasks stay
in the audit ledger. Runs live in a process-safe private `autopilot.db` and survive
server replacement. Stale owners become `interrupted` and are never silently
replayed.

The tool agent retains the complete evidence ledger while compacting only its
model-facing observation window. It repairs malformed JSON decisions twice,
blocks unchanged failing tool-call loops, and reserves a final-only synthesis
pass after the tool-step budget is exhausted. Negative existence claims receive
an extra local evidence review; explicit literals and named headings must be
searched exactly, and the host may run only a guarded read-only recovery action
before asking the model to synthesize again.

```text
/autopilot status [id]
/autopilot plan [--observe] [--no-web] [--static] <objective>
/autopilot run [--observe] [--no-web] [--static] <objective>
/autopilot resume|pause|cancel <id>
```

Workspace mode can inspect, create, and edit through bounded tools, but cannot
delete files, change accounts/permissions/memory, launch fleets, use cloud tiers,
or infer location. Observe mode is read-only. The Flutter System page exposes the
same goal composer, adaptive/static selector, checkpoint and replan counters,
persisted checklist, budgets, events, end report, and lifecycle controls. Use
`--static` to opt out of adaptive checkpoints. Starting, resuming, pausing, or
cancelling through hosted HTTP requires a developer/admin account; status remains
read-only.

### General artifact forge and greenfield games

`artifact_generate(name, brief)` turns a free-form request into a deterministic,
stdlib-only creative pack. It is not game-specific: requests can produce icons,
logos, backgrounds, textures, tilesets, sprite sheets, SVG vectors and diagrams,
brand palettes, Markdown briefs, editable DOCX reports, JSON/CSV sample data,
editable XLSX workbooks, standalone HTML mockups, editable PPTX slide decks, PCM
WAV sound effects and music loops, editable MIDI scores, animated GIF previews,
uncompressed AVI video with synchronized PCM audio, SRT/WebVTT captions,
CMX-style EDL timelines, OBJ/MTL models, self-contained textured and
rigged/animated GLB 2.0 characters, and JSON scenes. The generator uses deterministic in-house
OOXML/ZIP, RIFF/AVI, GIF/LZW, MIDI-event,
caption/timecode, PNG/PPM, waveform, and procedural-geometry writers, bounded
sizes, safe workspace paths, idempotent regeneration, and SHA-256 manifests.
`artifact_verify(path)` checks every file before downstream use.
Generation now fails closed unless every manifest size/hash and every recognized
format contract passes. `artifact_ground(path, recipe, requirements_json)` and
`/artifactcheck <path> [| recipe]` apply the same guarded recipes to arbitrary
workspace outputs: writing/Markdown structure, JSON fields, CSV columns and row
shape, DOCX paragraphs, XLSX rows/sheets, PPTX slides, OOXML relationships and
package safety, AVI streams/frames/index/audio synchronization, animated GIF
frames/LZW/duration, MIDI tracks/notes/tempo, caption timing and cue text, EDL
event/timecode continuity and local-media references, self-contained HTML/UI
references, SVG geometry, PNG chunks/CRCs, PPM dimensions, WAV frames/duration,
OBJ vertices/faces/index bounds, GLB containers/buffers/accessors/scenes,
embedded PNG CRCs/pixel streams, texture/sampler/material references, UV sets,
triangle indices, unit normals, joint indices, normalized skin weights,
inverse-bind matrices, animation timelines/quaternions, and complete bundle
manifests. Custom
requirements can pin headings, text, fields, columns, files, kinds, sizes,
paragraph/row/slide/frame/note/cue/event/vertex/triangle/joint/animation counts,
sheets, and dependency policy.

`game_reference_suite` is the known-good default baseline: persistent Python 2D,
JavaScript 2.5D, C++ 3D, and C# 2D projects consume generated assets, simulate
bounded gameplay, software-render `frame.ppm`, print `GAME_OK`, and exit. Verified
fallbacks cover every combination of Python, JavaScript, C++, and C# with 2D,
2.5D, and 3D labels. Projects resolve assets beside their script or executable,
so they remain runnable when launched from another working directory. The
model-driven `game_generate_and_test` and `game_generation_campaign` surfaces use
the same contract, reject third-party engine tokens and placeholders, compile/run
the candidate, repair failures, and record grounded outcomes. If the local model
cannot satisfy the contract, a clearly labeled verified reference fallback leaves
a runnable project while the model attempt remains recorded as failed.

Concrete `/master` requests containing an explicit build verb plus a game or
artifact target now route directly through these grounded forges. Language,
dimension, theme, and campaign intent are inferred conservatively; `fleet`,
`multiple`, and `various` requests use the hardware-bounded campaign path. Design
questions and explanation requests remain ordinary conversation instead of
creating files unexpectedly. Explicit campaign constraints are sticky: a C++
2.5D fleet stays C++/2.5D, while an unconstrained multi-language request rotates
through the verified default matrix. The direct command form is
`/gamefleet <name> | <concept> [| language | dimension]`.

---

## Recommended offload procedure

Use Trilobite as a local junior implementer, not as the final authority:

1. Start substantial work with `status()` or `diagnostics()` so you know model,
   VRAM, context, and memory-quality state before launching a batch.
2. Prefer `offload(tier="code")` for one self-contained coding draft,
   `run_code` / `parallel_generate_run` for bounded experiments, and
   `master_orchestrate` only when independent perspectives are actually useful.
   For repository tasks, `master_orchestrate` switches to guarded read-only
   tool agents and requires successful file evidence before accepting an answer.
   Allowed roots come from `TRILOBITE_FILE_ROOTS` plus the hot-read
   `file_roots.local`; denied/unavailable access falls back to EVIDENCE_REQUIRED.
3. Keep prompts narrow and complete. Include the relevant files, constraints,
   acceptance checks, and what "done" means; the local model cannot see the
   caller's hidden context.
4. Treat every answer as a draft. The caller must audit APIs, edge cases, file
   operations, security, and project style before applying changes.
5. Use agent fan-out deliberately: 1-3 agents for normal work and 4-6 for useful
   diversity. Explicit `fleet`, `swarm`, `workflow`, `parallel agents`, or
   spawn-as-many requests queue the hardware-derived breadth ceiling (two
   candidates per logical CPU, capped at 64 and overridable with
   `TRILOBITE_MAX_AGENTS`). A separate scheduler bounds simultaneous model calls
   from CPU and currently available RAM (8 automatic slots maximum). Inspect it
   with `/capacity [requested-agents]`; use `/agentcancel <id|prefix|all>` for
   cooperative cancellation. Queued work stops immediately. Already-running
   Ollama/HTTP calls return naturally and their late results are discarded. Fleet
   state is shared through a private per-user SQLite ledger, so the app, REPL, API,
   and MCP processes see the same work. Crashed/stale owners become `interrupted`
   after a heartbeat grace period and are never replayed automatically; explicitly
   rerun one with `/agentretry <master-id|prefix> [tier]` (local `code` by default).
6. Record outcomes only after grounded evidence: compile, tests, direct use, or
   explicit rejection. Good labels improve retrieval; vague labels pollute it.
7. Run `memory_quality_report()` periodically and
   `memory_quality_repair(apply=True)` after reviewing duplicate plans. Retrieval
   quality is part of model quality.
8. Keep cloud tiers opt-in. Local tiers are the default for private code and
   user-specific memory; cloud tiers are metered and prompts leave the machine.
9. Dogfood automatically. When normal Trilobite use reveals a bug, missing
   feature, weak procedure, confusing docs, bad default, flaky test, or other
   fixable issue in this repo, the assistant working with the user is authorized
   to implement the fix, run the relevant tests, commit, and push to
   `Krilliac/Trilobite-model` without waiting for a separate planning round.

Do not offload secrets, credential material, final security review, subtle
correctness decisions, hot-path performance work, or changes whose failure mode
cannot be checked locally. Do not auto-apply changes that weaken security,
broaden permissions/cloud access, delete user data, rewrite history, or require
privilege escalation beyond normal repository development; surface those for
explicit approval.

---

## Quickstart

**On a server (get the model live):**
```bash
git clone https://github.com/Krilliac/Trilobite-model.git && cd Trilobite-model
bash deploy_trilobite.sh            # installs Ollama, picks a model that fits your RAM, creates the alias
# or:  bash deploy_trilobite.sh --serve   # also host it as a public API service (prints URL + API key)
```

**Local dev:**
```bash
python -m venv venv && venv/Scripts/pip install mcp -r requirements-dev.txt
venv/Scripts/python -m pytest -q          # run the test suite
python trilobite_repl.py                  # interactive session
```

**Bundled desktop engine setup:** run `bootstrap-engine.cmd` or press **Setup
engine** in the app. It starts Ollama if needed, detects RAM, chooses a
`qwen2.5-coder` size, pulls embeddings, and creates the `trilobite` alias.

**Headless server/console mode:** the chat app is optional. On Windows run
`trilobite-headless.cmd start` to launch Ollama and `trilobite_serve.py`
without a visible app, `trilobite-headless.cmd status` to inspect it, and
`trilobite-headless.cmd stop` to stop the Trilobite API process. Use
`--stop-ollama` with stop if you also want to shut down the Ollama daemon.
Cross-platform equivalent:
`python trilobite_headless.py start --port 11435 --context-size 32k`.
Then connect with `trilobite_client.py`, any OpenAI-compatible client pointed at
`http://127.0.0.1:11435/v1`, the REPL, Claude MCP, or the Flutter app later.
HTTP clients used by more than one conversation or device should send a stable,
unique `session` value per conversation. Session state is isolated by the
authenticated principal plus that explicit ID; a blank session is intentionally
ephemeral and should not be used when multiple clients need continuity.
Calling `trilobite` from a Windows terminal now follows the same local-first
startup path as the app: it sets the shared state home, bootstraps the engine on
first run, starts the background API server, then opens the REPL. Set
`TRILOBITE_TERMINAL_BOOTSTRAP=0` to skip first-run setup or
`TRILOBITE_TERMINAL_START_SERVER=0` to open only the REPL. If
`TRILOBITE_SERVER` is set, the same command opens the hosted/API client after
warming up the local fallback server; set `TRILOBITE_TERMINAL_REMOTE=0` to force
the local REPL.

**Use the hosted model from any PC:** see [CLIENT.md](CLIENT.md).

**HTTP auth and hosted/admin mode:** `TRILOBITE_AUTH_MODE` accepts `api-key`,
`account`, `both`, or `either`. If it is unset, configuring
`TRILOBITE_API_KEY` selects API-key mode, `TRILOBITE_REQUIRE_ACCOUNT=1` selects
account mode, and a credential-free server remains `local-open` only on
loopback. `both` requires the API key in `Authorization: Bearer ...` and the
account session token in `X-Trilobite-Account-Token`; `either` accepts either
credential. A non-loopback bind is refused unless authentication is explicitly
strong: API keys must be at least 24 characters, account signing secrets in
`TRILOBITE_AUTH_SECRET` at least 32, and `both`/`either` require both strengths.

The first HTTP admin registration is a one-time bootstrap: configure a
`TRILOBITE_BOOTSTRAP_SECRET` of at least 16 characters and send it in
`X-Trilobite-Bootstrap-Secret`. Successful creation consumes that bootstrap.
Later HTTP registration is disabled unless `TRILOBITE_ALLOW_REGISTRATION=1`
and the request is authenticated as an admin. `/login` returns an account
session token; `/accounts` and `/setaccount` manage roles, tiers, dev flags and
bans. `/debug` exposes only safe inspectable state. `/cot` remains denied; use
`/trace`, `/debug`, `/agents`, retrieved lessons, tool calls and status logs.

Browser origins are denied unless they exactly match a comma-separated entry in
`TRILOBITE_CORS_ORIGINS`; `*` is intentionally ignored. HTTP POST bodies must
have `Content-Type: application/json` and an explicit `Content-Length`.
`TRILOBITE_MAX_REQUEST_BYTES` defaults to 1 MiB and is capped at 16 MiB.

**Workbench/filesystem tools:** guarded `/inventory`, `/tree`, `/search`, `/programs`,
`/scripts`, `/image`, `/mkdir`, `/runprogram`, `/runscript`, `/files`, `/read`,
`/write`, `/append`, `/edit`, and dry-run `/delete` operate inside approved
local roots. Program execution is argv-only (no shell command strings), bounded
by time/output limits, and kills timed-out process trees. Scripts use known
interpreters; direct inline PowerShell/cmd execution is rejected. The matching
server tools are available to agents and reusable workflows. The default roots
are the checkout and `TRILOBITE_HOME`;
`file_roots.local` now defaults to `TRILOBITE_HOME/file_roots.local`, not the
checkout. Override that file with `TRILOBITE_FILE_ROOTS_FILE`, or add roots with
`TRILOBITE_FILE_ROOTS`. Control-plane files (root policy/config/state files,
the memory database, credential-like files, and root-level Python modules) may
be read under the guarded policy but mutation requires an authenticated
developer/admin token; an approval code or broad root alone does not bypass
that protection. `TRILOBITE_FILE_APPROVAL_CODE` and
`TRILOBITE_FILE_BYPASS=1` remain local-owner controls for ordinary broader
paths. Deletes require the exact `DELETE <resolved path>` confirmation string
returned by the dry-run.

`workspace_inventory` reads metadata only and reports manifests, extension and
area sizes, largest files, exclusions, elapsed time, and a concrete truncation
reason. Inventory, text search, and script discovery enforce both an entry
budget and a wall-clock deadline, never follow symlinks, and skip hidden and
generated/tooling directories unless explicitly included.

**Selectable context:** use `/contextsize 32k`, `/contextsize 256k`, or
`/contextsize 1m` to select the requested virtual context. Ollama receives a
safe native `num_ctx` clamped by `TRILOBITE_NATIVE_CONTEXT_MAX` (default around
256k), while Trilobite represents larger budgets with summaries, retrieval,
facts, and recent-turn selection. App Settings has the same Context size field;
env defaults are `TRILOBITE_CONTEXT_SIZE`, `TRILOBITE_NATIVE_CONTEXT_MAX`, and
`TRILOBITE_VIRTUAL_CONTEXT_MAX`.

---

## GPU and thread tuning

trilobite sends Ollama local-runtime options on every local model call so it uses
the machine instead of idling on conservative defaults:

- `LOCAL_LLM_NUM_THREAD` - CPU threads per request. Defaults to all detected CPU
  threads (`%NUMBER_OF_PROCESSORS%` on Windows, `nproc` on Linux).
- `LOCAL_LLM_NUM_GPU` - model layers to offload to GPU. Defaults to `999`, which
  asks Ollama to place all supported layers on the GPU. Set `0` for CPU-only, or
  `auto`/`none` to let Ollama decide.
- `LOCAL_LLM_NUM_BATCH` - inference batch size. Defaults to `512`.
- `TRILOBITE_MAX_AGENTS` - queued breadth cap for delegated orchestration.
  Defaults to two candidates per logical CPU (minimum 16), hard-bounded at 64.
  Raising it adds perspectives; it does not increase simultaneous model calls.
- `TRILOBITE_PARALLEL_WORKERS` - explicit concurrent model-call override,
  bounded to 1..16 and never above the requested agent count. Without it,
  Trilobite derives 1..8 slots from CPU count and available physical RAM.
- `TRILOBITE_FLEET_DB` - optional path for the private process-shared fleet
  ledger. Defaults to `TRILOBITE_HOME/fleet.db`.
- `TRILOBITE_FLEET_HEARTBEAT` - set `0` to disable owner heartbeats (primarily
  for deterministic tests). Production defaults to a five-second heartbeat.
- `TRILOBITE_RUNTIME_POLICY` - optional path to the shared JSON policy for local
  aliases and execution lanes. By default it is
  `TRILOBITE_HOME/runtime_policy.json`. `LOCAL_LLM_FAST`, `LOCAL_LLM_CODE`,
  `LOCAL_LLM_CODE_LOCAL`, and `LOCAL_LLM_GENERAL` seed only its first creation.
- `OLLAMA_FLASH_ATTENTION` - enabled as `1` by the launch scripts when they start
  Ollama.

Check the active values with `diagnostics()` or REPL `/diagnostics`; check VRAM
residency with `status()`.

---

## Making it *yours*

A fresh trilobite is just base Qwen — you make it valuable by feeding it *your* world:
- **`/train`** — it practices real tasks, runs its own solutions to check them, and keeps the lessons from what works.
- **Endless training** — run `endless-train.cmd` on Windows to continuously generate,
  compile, execute, repair, and record passing multi-language campaign work until
  Ctrl+C or a no-progress round. Tune it with `TRILOBITE_ENDLESS_TOTAL`,
  `TRILOBITE_ENDLESS_LANGUAGES`, `TRILOBITE_ENDLESS_TIER`,
  `TRILOBITE_ENDLESS_WORKERS`, `TRILOBITE_ENDLESS_TIMEOUT`, and
  `TRILOBITE_ENDLESS_REPAIRS`.
- **Use it on your actual work** — the more real, grounded outcomes it sees, the more its lessons reflect *your* code and conventions (not textbook generalities).
- **Fine-tune** (optional) — once you've grown a real dataset, QLoRA bakes learning into the weights. Local 1.5B fits a 6 GB card; 7B wants a cloud GPU (`cloud_train.sh` + [TRAINING.md](TRAINING.md)). The result converts to a single GGUF file Ollama loads.

Keep your grounded, personal data **private** (it stays gitignored). The distilled, non-sensitive lessons can be exported to `lessons.jsonl`.

---

## Contributing improvements (without hosting the model)

The model and your raw interactions always stay local — only small, distilled **lesson text** ever travels, and only if you opt in:

1. **`contribute.py`** exports lessons that pass a conservative privacy scrub (no paths, secrets, or emails; short generic sentences only) to `contrib/lessons_contrib.jsonl`. Nothing is sent anywhere yet — review the file yourself.
2. **Send it home base** — open a PR adding your file under `contrib/`, or copy it to your own file server.
3. **CI aggregates** — `.github/workflows/aggregate-lessons.yml` dedupes everyone's `contrib/*.jsonl` into `community_lessons.jsonl` at the repo root. Scheduled runs are preview-only so they do not surprise-push over local GUI work; run the workflow manually when you want it to commit the updated file.
4. **`pull_community.py`** merges `community_lessons.jsonl` (fetched via `git pull` or your file server) back into your local `memory.db`, tagged `source_interaction='community'`.

Local maintenance uses the same conservative privacy rules as contribution
export. `/privacy` shows only redacted previews and stable lesson IDs;
`/privacyfix <ids>` is a dry run, while `/privacyfix apply <ids>` can delete only
the explicitly selected lessons that are still flagged. `/embeddings [N]`
previews missing vectors and `/embeddings apply [N]` backfills a bounded batch
through the configured local Ollama embedding model. Neither maintenance path
uses a cloud model.

Privacy is opt-in and scrubbed at every step — nothing auto-uploads, and no PR or upload happens without you reviewing it first.

---

## Roadmap (toward "for everyone")

**Shipped**
- ✅ **One-click engine setup** — bundled installs can bootstrap the local engine, detect available memory, pick a practical default model size, and start the local server without terminal setup.
- ✅ **Richer passive learning** — accepts explicit accept/use/copy/edit signals from the CLI, server, and GUI in addition to natural follow-up phrasing.
- ✅ **Local-first hosted opt-in** — cloud/hosted tiers are disabled unless `TRILOBITE_ALLOW_CLOUD=1` or the app setting is explicitly enabled.
- ✅ **General artifact grounding** — `ground_artifact` validates in-memory content, while `artifact_ground` and `/artifactcheck` validate guarded files and bundles with format-specific writing, editable Office, data, UI, image, audio, model, and manifest recipes. Generated packs must pass these contracts before success is reported.
- ✅ **Editable Office deliverables** — deterministic stdlib-only DOCX reports, XLSX workbooks, and PPTX decks are inferred from natural-language requests, open in installed Microsoft Office, and fail closed on unsafe ZIP paths, malformed XML, missing relationships, active content, external dependencies, or unmet content requirements.
- ✅ **Editable media and timelines** — deterministic animated GIFs, MIDI scores, SRT/WebVTT captions, and EDL edit timelines are inferred from media requests, independently parseable, and grounded for real frames, notes, timing, cue text, and non-overlapping edits.
- ✅ **Self-contained video containers** — deterministic RIFF/AVI previews combine real 24-bit frames with synchronized PCM audio, carry a complete seek index, open in the native Windows video stack, and serve as the local source media for generated EDL timelines.
- ✅ **Passive learning** — infers outcomes from natural follow-up ("that worked" / "no, still errors") so it learns without manual scoring.
- ✅ **Personas** — `/persona coder|explainer|reviewer|teacher` so non-coders get value too.
- ✅ **Federated contribution** — share scrubbed lessons back without hosting the model (see [above](#contributing-improvements-without-hosting-the-model)).
- ✅ **Mobile & desktop app (GUI)** — a [Flutter client](app/) with a real chat UI (Android APK + Windows/Linux/macOS), CI-built with download links. No terminal needed to *use* a hosted trilobite.

- ✅ **Textured rigged animated 3D models** — deterministic binary glTF 2.0 characters carry indexed geometry, unit normals, orthogonal tangent frames, UVs, embedded power-of-two base-color, packed occlusion/roughness/metallic, and tangent-space normal PNGs, complete PBR texture bindings, a two-joint skin with inverse-bind matrices, normalized per-vertex weights, and a looping quaternion animation in one dependency-free GLB.

**Next gaps** — make the bundled engine downloader fully self-contained per platform and add morph targets, richer skeletal clips, and broader native viewer/render compatibility beyond the current deterministic textured model contract.

---

## Architecture

Flat, mostly-stdlib Python modules (plus `mcp`):

| module | role |
|---|---|
| `memory_store.py` | SQLite + FTS5 store (interactions, outcomes, lessons, visible tasks/todos) |
| `embeddings.py` | local Ollama embeddings + cosine (soft-fail) |
| `retriever.py` | hybrid lexical+semantic retrieval with relevance threshold |
| `reward.py` / `reflection.py` | outcome → score; distill deduped lessons |
| `orchestrator.py` | the retrieve → augment → generate → capture flow |
| `master_orchestrator.py` / `fleet_store.py` | RAM/CPU-bounded fleet execution plus a process-shared restart/recovery ledger |
| `autopilot_controller.py` / `autopilot_store.py` | Persistent local goal planning, guarded execution/review, evidence gates, budgets, and explicit lifecycle control |
| `runtime_policy.py` | Atomic per-user local-model aliases and execution-lane routing shared across live surfaces |
| `reloadable_mcp.py` | Fail-closed live server-source execution, atomic tool-manager swaps, schema-cache invalidation, and MCP tool-list notifications |
| `learning_health.py` | Outcome coverage, reward distribution, lesson provenance, distillation yield, and memory-hygiene reporting |
| `artifact_grounding.py` | Guarded format contracts for writing, data, UI, images, audio, models, and complete artifact bundles |
| `model_assets.py` | Deterministic stdlib-only binary glTF geometry, embedded textures, PBR materials, skinning, inverse-bind matrices, and animation |
| `creative_router.py` | conservative natural-language routing from concrete master build requests into grounded artifact, game, or game-campaign workflows |
| `server.py` / `workbench.py` / `activity_tracker.py` / `code_runner.py` / `web_tools.py` / `workflow_store.py` / `self_heal.py` | MCP workbench/agent tools, guarded discovery and execution, persistent checklists, exact action/end reports, bounded code/project runners, web tools, workflows, and self-healing |
| `server.py` | MCP server: `offload` / `trilobite` / `parallel_run_code` / `parallel_generate_run` / `parallel_generate_run_languages` / `campaign_generate_compile_execute_record` / `learn_tiers` / `record_outcome` / `trilobite_stats` / `trilobite_sessions` / `trilobite_remember_fact` |
| `assetgen.py` / `game_forge.py` | stdlib-only general artifact generation, manifest verification, portable cross-language game projects, model campaigns, and verified 4-language x 3-dimension fallbacks |
| `recall.py` | semantic recall of past good-outcome solutions (vector search over interactions) |
| `summarizer.py` | rolling conversation summaries + session auto-titles (fast tier) |
| `trilobite_repl.py` / `trilobite_client.py` | local REPL / thin remote client |
| `trilobite_serve.py` | OpenAI-compatible proxy (for chat UIs) |
| `intents.py`, `grounding.py`, `training_tasks.py`, `self_curriculum.py`, `eval_retrieval.py`, `game_ladder.py` | NL control, sandboxed execution, practice tasks, self-generated curriculum, retrieval eval, capability gauntlet |
| `qlora_train.py`, `export_training_data.py`, `cloud_train.sh` | fine-tuning pipeline |

---

## Live reload

Long-running `trilobite_serve.py` and `trilobite_repl.py` processes check for source edits before each request/turn. Edits to `server.py` and helper modules such as personas, retrieval, summarization, feedback, and code execution are picked up on the next call without hard restarting the proxy or REPL. Set `TRILOBITE_LIVE_RELOAD=0` to disable this.

The MCP server now stages the complete updated `server.py` tool registry in an
isolated namespace at the next list/tool request. A clean load atomically swaps
implementations and schemas, clears the protocol schema cache, and emits
`notifications/tools/list_changed`; a syntax/import/runtime failure discards the
stage and keeps every last known-good tool active. This covers changed, added,
and removed MCP tools without disconnecting the process. The first upgrade from
an older startup-bound build still needs one reconnect so this reloadable kernel
itself is loaded. Inspect convergence with `/mcp`, `mcp_runtime_status()`, or
`live_reload_status()`.

## Shared Runtime State

Multiple installs can run the same system code, but they should not each own a
separate memory database. Runtime state defaults to one per-user home directory:
`%LOCALAPPDATA%\trilobite` on Windows, `$XDG_DATA_HOME/trilobite` or
`~/.local/share/trilobite` on Linux, and the matching app data home on macOS.
Set `TRILOBITE_HOME` to force a specific shared state folder, or `TRILOBITE_DB`
to point directly at a database file. If an older install has `memory.db` beside
the code and the shared DB does not exist yet, trilobite copies it into the
shared home on first run.

The same home contains `runtime_policy.json`, the single source of truth for
local model aliases and automatic execution lanes across MCP, the HTTP server,
REPL, workbench, Autopilot, fleet workers, and review. Every request boundary
reloads it, so an atomic update from one running surface is visible to the others
without a hard restart. Inspect it with `/runtime` or
`runtime_policy_status()`. Developer/admin-authorized edits use, for example,
`/runtime set general=qwen2.5:7b-instruct review=general`; `/runtime reset`
restores safe defaults. Updates reject cloud-looking or uninstalled model names.
The policy cannot enable cloud tiers, broaden permissions/roots, or store
credentials. The Flutter System page renders the same revision, aliases, lanes,
path, and any missing-model warning alongside MCP source/tool convergence.

## Standing instructions

`system_profile.md` is an editable Markdown profile injected into every `trilobite` / OpenAI-proxy answer. Edit the file directly, or use `system_profile_text()` and `update_system_profile(mode="append"|"replace"|"clear")` through MCP. Because the profile is read at request time, changes apply on the next call.

## Emotion Vectors

`emotion_vectors.json` holds live tone-steering dimensions from `-1.0` to `+1.0` such as warmth, calm, curiosity, confidence, playfulness, urgency, skepticism, and brevity. These are behavioral controls, not claims about internal feelings. Edit the JSON directly, or use `emotion_vector_status()` and `update_emotion_vectors(mode="merge"|"replace"|"clear")`; changes apply on the next call.

## Workflows And Memory Tools

`workflows.json` stores reusable `loop` action lists. Use `workflow_save()` to keep a routine, `workflow_run()` to execute it, and `workflow_list()` / `workflow_delete()` to manage it. The built-in `status_sweep` workflow checks diagnostics, profile, emotion vectors, and Ollama state.

The MCP surface also includes `memory_search()`, `memory_export()`, and `session_export()` so the model can inspect local lessons, facts, sessions, and transcripts without raw SQLite access. `tool_manifest()` prints a compact map of the available tools.

`learn_from_example()` lets you teach from a known-good task/solution pair, while `apply_learned()` shows which lessons would be applied to a new task. Lesson usage is tracked: when a retrieved lesson participates in an answer and you later call `record_outcome()`, the lesson gets credited or debited so future retrieval can prefer lessons that actually helped.

## Agentic Tool Use And Web

`workbench_agent(prompt)` runs a bounded local tool-use loop with four enforced
phases: inspect, implement/analyze, validate, report. The host rejects mutation
before inspection and does not accept an equivalent in-memory snippet as proof
that an edited disk file works; scripts must be run from their persistent path
or covered by a real workspace test/build. `agent()` exposes the same tools with
optional checklist behavior. Both can inspect trees/files/programs/scripts/images,
mutate guarded files, generate arbitrary asset packs or games, execute bounded
checks, use memory/workflows/web, and return exact observable evidence.

`web_search()` and `web_fetch()` use stdlib HTTP only. Search defaults to
DuckDuckGo HTML, or set `TRILOBITE_SEARCH_URL` to an endpoint containing
`{query}`. `weather_lookup()` resolves a supplied city/postal code and gets
sourced current conditions plus a short forecast from Open-Meteo. Ordinary chat
now routes explicit weather/current-web requests through these tools instead of
letting the base model falsely claim that internet access is unavailable.

Approximate location remains opt-in. The Flutter Settings switch contacts
`ipwho.is` only for a location-dependent prompt, minimizes the response to
city/region/country/timezone, discards coordinates, and never sends the raw IP
to Trilobite.
The chat response identifies the resolved place as an approximate public-IP
estimate because VPN and ISP routing can make it wrong. Loopback clients can use
the same consented server-side fallback through `approximate_location_lookup()`;
remote hosted clients must provide the client-side hint so the server's data
center location is not mistaken for the user's. Set `TRILOBITE_WEB_TOOLS=0` to
disable all web and location access.

## Self Healing

`self_heal_check()` looks for common local breakage: invalid JSON config files, live-reload errors, broken venv pointers, and lesson-store integrity issues. `self_heal_repair(apply=True)` only applies conservative local fixes: rebuild missing lesson FTS rows, remove orphan FTS rows, clear corrupt lesson embeddings, and restore default JSON config files after making backups. Python/venv problems and syntax errors are reported for manual repair.

## Status & honest caveats

- Research/hobby project, built with heavy AI assistance; not a polished product.
- A local 7B trails frontier hosted models on raw capability — by design; the trade is privacy + ownership.
- Lesson-retrieval's measured benefit is real only when relevant lessons exist; on easy tasks it's do-no-harm, not a boost. Real gains come from *your* accumulated data and/or fine-tuning.
- Tests run in CI on every push. Local-first throughout; your private data stays yours.
- `python scripts/ci_watch.py` shows the latest GitHub Actions runs through the
  GitHub CLI, which makes failed build/debug loops faster from a local checkout.
