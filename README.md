# trilobite

<!-- ci-artifact-badges:start -->
[![Latest CI artifacts 4 files](https://img.shields.io/badge/Latest%20CI%20artifacts-4%20files-2088FF?style=for-the-badge&logo=githubactions&logoColor=white)](https://github.com/Krilliac/Trilobite-model/actions/runs/28946614755)
[![Android download](https://img.shields.io/badge/Android-download-3DDC84?style=for-the-badge&logo=android&logoColor=white)](https://github.com/Krilliac/Trilobite-model/actions/runs/28946614755/artifacts/8169968387)
[![Linux download](https://img.shields.io/badge/Linux-download-FCC624?style=for-the-badge&logo=linux&logoColor=black)](https://github.com/Krilliac/Trilobite-model/actions/runs/28946614755/artifacts/8169889318)
[![Windows download](https://img.shields.io/badge/Windows-download-0078D4?style=for-the-badge&logo=windows&logoColor=white)](https://github.com/Krilliac/Trilobite-model/actions/runs/28946614755/artifacts/8169935175)
[![macOS download](https://img.shields.io/badge/macOS-download-000000?style=for-the-badge&logo=apple&logoColor=white)](https://github.com/Krilliac/Trilobite-model/actions/runs/28946614755/artifacts/8169918596)
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
tiers learn is configurable (`TRILOBITE_LEARN_TIERS`, default all tiers:
`fast,code,general,cloud-code,cloud-general`). The memory, capture, and distillation always stay local; only a
cloud-tier *prompt* leaves the machine, and only when you choose a cloud model.
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

---

## Three ways to run it

1. **Local, in your terminal** — `trilobite` (like launching `claude`). Interactive REPL routed through the full loop, with `/trace`, `/strict`, `/run`, `/train`, `/pass`, `/fail`, `/stats`, `/context`, `/quality` commands plus conversation commands `/new`, `/sessions`, `/resume`, `/project`, `/fact`, `/facts` (and plain-English equivalents). Each REPL launch is its own remembered thread.
2. **Hosted on your own server + a thin client anywhere** — run `deploy_trilobite.sh --serve` on your box (systemd service, API key), then any machine runs the single-file `trilobite_client.py` pointed at it. The serve layer threads the chat UI's own conversation history.
3. **Integrated with Claude** — the MCP `local-llm` tools let Claude offload to it (`agent`, `offload(learn=True)`, `trilobite`, `run_code`, `web_search`, `web_fetch`, `loop`, `workflow_list/save/run/delete`, `self_heal_check`, `self_heal_repair`, `learn_from_example`, `apply_learned`, `system_profile_text`, `update_system_profile`, `emotion_vector_status`, `update_emotion_vectors`, `memory_search`, `memory_export`, `session_export`, `memory_quality_report`, `memory_quality_repair`, `tool_manifest`, `context_health`, `diagnostics`, `live_reload_status`, `record_outcome`, `trilobite_stats`, `trilobite_sessions`, `trilobite_remember_fact`). `agent` runs a Claude-like tool-use loop where the model can call local tools and web tools step-by-step; `run_code` gives bounded local execution for Python, JavaScript/Node, and PowerShell snippets; `loop` repeats bounded code/model/system actions for retries, polling, and small workflows. Both `offload` and `trilobite` take a `tier` to route a call to any configured model (local or a paid cloud model); cloud tiers answer clean and still feed the learning loop.
3. **Integrated with Claude** — the MCP `local-llm` tools let Claude offload to it (`offload(learn=True)`, `trilobite`, `parallel_run_code`, `parallel_generate_run`, `parallel_generate_run_languages`, `campaign_generate_compile_execute_record`, `learn_tiers`, `record_outcome`, `trilobite_stats`, `trilobite_sessions`, `trilobite_remember_fact`). `parallel_run_code` compiles/runs many snippets at once across Python, JavaScript, PowerShell, C++, and C#; `parallel_generate_run` asks the model for multiple Python candidates in parallel; `parallel_generate_run_languages` spreads candidates across several languages, compiles/runs them, and returns passing winners. `campaign_generate_compile_execute_record` runs a bounded self-improvement campaign, repairs failures, and records passing interactions as grounded lessons. Both `offload` and `trilobite` take a `tier` to route a call to any configured model (local or a paid cloud model); cloud tiers answer clean and still feed the learning loop.
4. **Mobile & desktop app (GUI)** — a cross-platform [Flutter client](app/) that talks to a hosted `trilobite_serve.py`. One codebase → an **Android APK** and **Windows/Linux/macOS** desktop apps, built in CI with downloadable installers. See [app/README.md](app/README.md).

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

**Use the hosted model from any PC:** see [CLIENT.md](CLIENT.md).

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
3. **CI aggregates** — `.github/workflows/aggregate-lessons.yml` dedupes everyone's `contrib/*.jsonl` into `community_lessons.jsonl` at the repo root (weekly + manual).
4. **`pull_community.py`** merges `community_lessons.jsonl` (fetched via `git pull` or your file server) back into your local `memory.db`, tagged `source_interaction='community'`.

Privacy is opt-in and scrubbed at every step — nothing auto-uploads, and no PR or upload happens without you reviewing it first.

---

## Roadmap (toward "for everyone")

**Shipped**
- ✅ **Passive learning** — infers outcomes from natural follow-up ("that worked" / "no, still errors") so it learns without manual scoring.
- ✅ **Personas** — `/persona coder|explainer|reviewer|teacher` so non-coders get value too.
- ✅ **Federated contribution** — share scrubbed lessons back without hosting the model (see [above](#contributing-improvements-without-hosting-the-model)).
- ✅ **Mobile & desktop app (GUI)** — a [Flutter client](app/) with a real chat UI (Android APK + Windows/Linux/macOS), CI-built with download links. No terminal needed to *use* a hosted trilobite.

**Planned** — honest gaps between "great for tinkerers" and "usable by anyone":
- **One-click *engine* bundle** — the GUI now exists; the remaining piece is bundling the server/engine itself so there's no terminal setup on the host either (auto-detect hardware, auto-pick model size).
- **Richer passive learning** — capture edits/accepts, not just follow-up phrasing.
- **Optional hosted mode** for people with no capable hardware — *opt-in only*, because it trades away the privacy promise.
- **Beyond code** — generalize the grounding signal past "did it compile" to other domains.

---

## Architecture

Flat, mostly-stdlib Python modules (plus `mcp`):

| module | role |
|---|---|
| `memory_store.py` | SQLite + FTS5 store (interactions, outcomes, lessons) |
| `embeddings.py` | local Ollama embeddings + cosine (soft-fail) |
| `retriever.py` | hybrid lexical+semantic retrieval with relevance threshold |
| `reward.py` / `reflection.py` | outcome → score; distill deduped lessons |
| `orchestrator.py` | the retrieve → augment → generate → capture flow |
| `server.py` / `code_runner.py` / `web_tools.py` / `workflow_store.py` / `self_heal.py` / `live_reload.py` / `system_profile.py` / `emotion_vectors.py` | MCP server tools: `agent` / `offload` / `trilobite` / `run_code` / `web_search` / `web_fetch` / `loop` / workflows / memory export/search / self-healing / editable profile / emotion vectors / diagnostics; bounded execution, tool-calling loops, web access, reusable action routines, and request-boundary source reload |
| `server.py` | MCP server: `offload` / `trilobite` / `parallel_run_code` / `parallel_generate_run` / `parallel_generate_run_languages` / `campaign_generate_compile_execute_record` / `learn_tiers` / `record_outcome` / `trilobite_stats` / `trilobite_sessions` / `trilobite_remember_fact` |
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
