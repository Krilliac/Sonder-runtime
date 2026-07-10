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
The `code` tier is protected as the local coder lane: if `LOCAL_LLM_CODE` is
accidentally pointed at a cloud model, trilobite falls back to the local
`qwen2.5-coder:7b` model (override that local fallback with
`LOCAL_LLM_CODE_LOCAL`).

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
  calls, active response count, file creates/edits/deletes, and line deltas.
  Use `/activity` or watch the chat app's bottom status line while work runs.

---

## Four ways to run it

1. **Local, in your terminal** — `trilobite` (like launching `claude`). Interactive REPL routed through the full loop, with `/trace`, `/strict`, `/run`, `/runwindow`, `/train`, `/master`, `/agents`, `/asset`, `/forge`, `/game`, `/gamefleet`, `/todo`, `/commands`, `/dump`, `/permissions`, `/compact`, `/debug`, `/admin`, `/login`, `/register`, `/pass`, `/fail`, `/stats`, `/context`, `/quality`, `/emotion`, `/improve` commands plus conversation commands `/new`, `/sessions`, `/resume`, `/project`, `/fact`, `/facts` (and plain-English equivalents). Each REPL launch is its own remembered thread.
2. **Hosted on your own server + a thin client anywhere** — run `deploy_trilobite.sh --serve` on your box (systemd service, API key), then any machine runs the single-file `trilobite_client.py` pointed at it. The serve layer threads the chat UI's own conversation history.
3. **Integrated with Claude** — the MCP `local-llm` tools let Claude offload to it (`agent`, `master_orchestrate`, `master_status`, `artifact_generate`, `artifact_verify`, `game_reference_suite`, `game_generate_and_test`, `game_generation_campaign`, `task_create/list/update/show`, `command_registry_list`, `permission_policy`, `context_compaction_plan`, `offload(learn=True)`, `trilobite`, `run_code`, `parallel_run_code`, `parallel_generate_run`, `campaign_generate_compile_execute_record`, `web_search`, `web_fetch`, `loop`, `workflow_list/save/run/delete`, `self_heal_check`, `learn_from_example`, `apply_learned`, `system_improvement_report`, `memory_search`, `memory_quality_report`, `tool_manifest`, `diagnostics`, `record_outcome`, `trilobite_stats`). `agent` runs a local tool-use loop; `master_orchestrate` can delegate to parallel subagents and audit their outputs; artifact and game tools create persistent assets/projects and accept only grounded compile/run results. Both `offload` and `trilobite` can route to configured local or explicitly enabled cloud tiers.
4. **Mobile & desktop app (GUI)** — a cross-platform [Flutter client](app/) that talks to a hosted `trilobite_serve.py`. One codebase → an **Android APK** and **Windows/Linux/macOS** desktop apps, built in CI with downloadable installers. See [app/README.md](app/README.md).

### General artifact forge and greenfield games

`artifact_generate(name, brief)` turns a free-form request into a deterministic,
stdlib-only creative pack. It is not game-specific: requests can produce icons,
logos, backgrounds, textures, tilesets, sprite sheets, SVG vectors and diagrams,
brand palettes, Markdown briefs, JSON/CSV sample data, standalone HTML mockups,
PCM WAV sound effects and music loops, OBJ/MTL models, and JSON scenes. The
generator uses manual PNG/PPM encoding, waveform synthesis, procedural geometry,
bounded sizes, safe workspace paths, idempotent regeneration, and SHA-256
manifests. `artifact_verify(path)` checks every file before downstream use.

`game_reference_suite` is the known-good baseline: persistent Python 2D,
JavaScript 2.5D, C++ 3D, and C# 2D projects consume generated assets, simulate
bounded gameplay, software-render `frame.ppm`, print `GAME_OK`, and exit. The
model-driven `game_generate_and_test` and `game_generation_campaign` surfaces use
the same contract, reject third-party engine tokens and placeholders, compile/run
the candidate, repair failures, and record grounded outcomes. If the local model
cannot satisfy the contract, a clearly labeled verified reference fallback leaves
a runnable project while the model attempt remains recorded as failed.

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
   spawn-as-many requests use the hardware-derived ceiling (two submission slots
   per logical CPU, capped at 64 and overridable with `TRILOBITE_MAX_AGENTS`).
   Ollama still schedules actual GPU execution, so fan-out is bounded submission,
   not a promise that every model fits in VRAM simultaneously.
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

**Filesystem tools:** guarded `/files`, `/read`, `/write`, `/append`, `/edit`,
and dry-run `/delete` operate inside approved local roots. The server tools
`file_find/read/write/edit/delete` expose the same policy to agents and
workflows. The default roots are the checkout and `TRILOBITE_HOME`;
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
- `TRILOBITE_MAX_AGENTS` - delegated `master_orchestrate` cap. Defaults to `16`
  and is hard-bounded at `64`; raising it is for deliberate stress or very small
  prompts, not a default speed knob.
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

Privacy is opt-in and scrubbed at every step — nothing auto-uploads, and no PR or upload happens without you reviewing it first.

---

## Roadmap (toward "for everyone")

**Shipped**
- ✅ **One-click engine setup** — bundled installs can bootstrap the local engine, detect available memory, pick a practical default model size, and start the local server without terminal setup.
- ✅ **Richer passive learning** — accepts explicit accept/use/copy/edit signals from the CLI, server, and GUI in addition to natural follow-up phrasing.
- ✅ **Local-first hosted opt-in** — cloud/hosted tiers are disabled unless `TRILOBITE_ALLOW_CLOUD=1` or the app setting is explicitly enabled.
- ✅ **General artifact grounding** — `ground_artifact` validates non-code outputs with contains, regex, exact text, JSON, and JSON-field checks so learning is not limited to compile/run results.
- ✅ **Passive learning** — infers outcomes from natural follow-up ("that worked" / "no, still errors") so it learns without manual scoring.
- ✅ **Personas** — `/persona coder|explainer|reviewer|teacher` so non-coders get value too.
- ✅ **Federated contribution** — share scrubbed lessons back without hosting the model (see [above](#contributing-improvements-without-hosting-the-model)).
- ✅ **Mobile & desktop app (GUI)** — a [Flutter client](app/) with a real chat UI (Android APK + Windows/Linux/macOS), CI-built with download links. No terminal needed to *use* a hosted trilobite.

**Next gaps** — make the bundled engine downloader fully self-contained per platform, add more GUI visualizers for learning quality, and broaden artifact grounding recipes for writing, data, docs, and UI work.

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
| `server.py` / `code_runner.py` / `web_tools.py` / `workflow_store.py` / `self_heal.py` / `live_reload.py` / `system_profile.py` / `emotion_vectors.py` / `command_registry.py` / `permission_rules.py` | MCP server tools: `agent` / `offload` / `trilobite` / visible tasks / command registry / permission policy / context compaction plans / `run_code` / `web_search` / `web_fetch` / `loop` / workflows / memory export/search / self-healing / editable profile / emotion vectors / diagnostics; bounded execution, tool-calling loops, web access, reusable action routines, and request-boundary source reload |
| `server.py` | MCP server: `offload` / `trilobite` / `parallel_run_code` / `parallel_generate_run` / `parallel_generate_run_languages` / `campaign_generate_compile_execute_record` / `learn_tiers` / `record_outcome` / `trilobite_stats` / `trilobite_sessions` / `trilobite_remember_fact` |
| `assetgen.py` / `game_forge.py` | stdlib-only general artifact generation, manifest verification, persistent cross-language game projects, model campaigns, and verified reference fallbacks |
| `recall.py` | semantic recall of past good-outcome solutions (vector search over interactions) |
| `summarizer.py` | rolling conversation summaries + session auto-titles (fast tier) |
| `trilobite_repl.py` / `trilobite_client.py` | local REPL / thin remote client |
| `trilobite_serve.py` | OpenAI-compatible proxy (for chat UIs) |
| `intents.py`, `grounding.py`, `training_tasks.py`, `self_curriculum.py`, `eval_retrieval.py`, `game_ladder.py` | NL control, sandboxed execution, practice tasks, self-generated curriculum, retrieval eval, capability gauntlet |
| `qlora_train.py`, `export_training_data.py`, `cloud_train.sh` | fine-tuning pipeline |

---

## Live reload

Long-running `trilobite_serve.py` and `trilobite_repl.py` processes check for source edits before each request/turn. Edits to `server.py` and helper modules such as personas, retrieval, summarization, feedback, and code execution are picked up on the next call without hard restarting the proxy or REPL. Set `TRILOBITE_LIVE_RELOAD=0` to disable this.

For the MCP server, existing tool handlers also refresh helper modules at tool boundaries. Brand-new MCP tool names still require reconnecting/restarting the MCP server, because FastMCP registers the tool list once at startup. Use `live_reload_status()` to see what a running process is watching.

## Shared Runtime State

Multiple installs can run the same system code, but they should not each own a
separate memory database. Runtime state defaults to one per-user home directory:
`%LOCALAPPDATA%\trilobite` on Windows, `$XDG_DATA_HOME/trilobite` or
`~/.local/share/trilobite` on Linux, and the matching app data home on macOS.
Set `TRILOBITE_HOME` to force a specific shared state folder, or `TRILOBITE_DB`
to point directly at a database file. If an older install has `memory.db` beside
the code and the shared DB does not exist yet, trilobite copies it into the
shared home on first run.

## Standing instructions

`system_profile.md` is an editable Markdown profile injected into every `trilobite` / OpenAI-proxy answer. Edit the file directly, or use `system_profile_text()` and `update_system_profile(mode="append"|"replace"|"clear")` through MCP. Because the profile is read at request time, changes apply on the next call.

## Emotion Vectors

`emotion_vectors.json` holds live tone-steering dimensions from `-1.0` to `+1.0` such as warmth, calm, curiosity, confidence, playfulness, urgency, skepticism, and brevity. These are behavioral controls, not claims about internal feelings. Edit the JSON directly, or use `emotion_vector_status()` and `update_emotion_vectors(mode="merge"|"replace"|"clear")`; changes apply on the next call.

## Workflows And Memory Tools

`workflows.json` stores reusable `loop` action lists. Use `workflow_save()` to keep a routine, `workflow_run()` to execute it, and `workflow_list()` / `workflow_delete()` to manage it. The built-in `status_sweep` workflow checks diagnostics, profile, emotion vectors, and Ollama state.

The MCP surface also includes `memory_search()`, `memory_export()`, and `session_export()` so the model can inspect local lessons, facts, sessions, and transcripts without raw SQLite access. `tool_manifest()` prints a compact map of the available tools.

`learn_from_example()` lets you teach from a known-good task/solution pair, while `apply_learned()` shows which lessons would be applied to a new task. Lesson usage is tracked: when a retrieved lesson participates in an answer and you later call `record_outcome()`, the lesson gets credited or debited so future retrieval can prefer lessons that actually helped.

## Agentic Tool Use And Web

`agent(prompt, allow_web=True)` runs a bounded local tool-use loop. On each step the model returns a JSON tool call, the server executes it, then the observation is fed back until the model returns a final answer. Available tools include `run_code`, `web_search`, `web_fetch`, `memory_search`, `workflow_run`, `diagnostics`, `status`, profile/vector inspection, and `offload`.

`web_search()` and `web_fetch()` use stdlib HTTP only. Search defaults to DuckDuckGo HTML, or set `TRILOBITE_SEARCH_URL` to an endpoint containing `{query}`. Set `TRILOBITE_WEB_TOOLS=0` to disable web access.

## Self Healing

`self_heal_check()` looks for common local breakage: invalid JSON config files, live-reload errors, broken venv pointers, and lesson-store integrity issues. `self_heal_repair(apply=True)` only applies conservative local fixes: rebuild missing lesson FTS rows, remove orphan FTS rows, clear corrupt lesson embeddings, and restore default JSON config files after making backups. Python/venv problems and syntax errors are reported for manual repair.

## Status & honest caveats

- Research/hobby project, built with heavy AI assistance; not a polished product.
- A local 7B trails frontier hosted models on raw capability — by design; the trade is privacy + ownership.
- Lesson-retrieval's measured benefit is real only when relevant lessons exist; on easy tasks it's do-no-harm, not a boost. Real gains come from *your* accumulated data and/or fine-tuning.
- Tests run in CI on every push. Local-first throughout; your private data stays yours.
- `python scripts/ci_watch.py` shows the latest GitHub Actions runs through the
  GitHub CLI, which makes failed build/debug loops faster from a local checkout.
