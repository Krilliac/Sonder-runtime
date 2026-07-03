# trilobite

A self-improving, fully **local** coding assistant. It's a frozen Ollama model
(Qwen2.5-Coder) wrapped in a small Python orchestrator that retrieves
distilled "lessons" from past work, injects them into new prompts, captures
every interaction, scores outcomes you report, and distills new lessons from
the good ones. Everything — inference, memory, embeddings — runs on your own
machine; nothing is sent anywhere unless you deliberately use a cloud tier.

This started as `local-llm`, a small MCP bridge that lets [Claude
Code](https://claude.com/claude-code) offload subtasks to a local GPU model.
trilobite is the self-learning layer built on top of it. Both live in this
repo.

> **Status:** research/hobby project, built with heavy AI assistance. It
> works and is tested (89 unit tests), but the core claim — that lesson
> retrieval measurably improves output quality — is **not proven**; see
> [Status & honest caveats](#status--honest-caveats).

## Contents

- [What it is](#what-it-is)
- [How the loop works](#how-the-loop-works)
- [Interfaces](#interfaces)
- [Install / run](#install--run)
- [Self-training](#self-training)
- [Weight training (QLoRA)](#weight-training-qlora)
- [Architecture](#architecture)
- [Status & honest caveats](#status--honest-caveats)

## What it is

- **The model never changes.** Ollama always serves frozen weights (a
  Qwen2.5-Coder base, or the `trilobite` Modelfile alias pointing at it).
  All "learning" happens in the Python layer that wraps it.
- **Memory, not fine-tuning (by default).** Past interactions and their
  real-world outcomes are stored in a local SQLite database. Good outcomes get
  distilled into short, reusable "lessons" that get retrieved and injected
  into future prompts on related tasks. This is the inference-time learning
  loop, and it's the part that actually runs today.
- **Weight fine-tuning is a separate, optional, not-yet-run pipeline**
  (QLoRA) — prepared and tested to parse, but never executed end-to-end. See
  [Weight training (QLoRA)](#weight-training-qlora).
- **Local-only.** Local tiers (`fast` / `code` / `general`) run entirely on
  your GPU via Ollama. There are also optional Ollama-hosted `cloud-*` tiers
  for when you explicitly want frontier-size models — those are metered and
  the prompt leaves the machine. trilobite's own learning loop never uses the
  cloud tiers; captured/private code stays local.

## How the loop works

```
task ─▶ retrieve lessons ─▶ augment prompt ─▶ generate ─▶ capture ─▶ (you) record outcome ─▶ distill lesson
          (hybrid search)                                                 │
                                                                    good outcome only
                                                                            ▼
                                                                     dedup + store
```

1. **Retrieve.** `retriever.py` does hybrid lexical + semantic search over
   stored lessons: FTS5 keyword match (SQLite) fused with cosine similarity
   over embeddings (a local `nomic-embed-text` model via Ollama), combined
   with reciprocal rank fusion. Critically, a **relevance threshold**
   (`min_sim`, default `0.65`, tunable via `TRILOBITE_MIN_SIM`) filters the
   fused candidates — only lessons that are actually on-topic for the current
   task get injected. This threshold was added after an eval run showed
   irrelevant lessons being retrieved and hurting rather than helping (see
   [Self-training](#self-training)).
2. **Augment.** Retrieved lesson text is prepended to the prompt under a
   `# Relevant lessons from past work (may help):` header (`orchestrator.py`).
3. **Generate.** The augmented prompt goes to the frozen model. The response,
   task, and retrieved context are logged to `memory.db` under a new
   interaction id.
4. **Capture.** Every call through the learning path is logged before you
   know the outcome — `interactions(id, task, retrieved_ctx, response, tier, ts)`.
5. **You record the outcome.** Once you know how it went (tests passed,
   accepted, compiled, rejected, failed), you call `record_outcome`. This
   writes to `outcomes` and maps the signal to a scalar reward
   (`reward.py`).
6. **Distill.** Outcomes at or above the "good" threshold (`tests_passed`,
   `accepted`, `compiled`) trigger `reflection.py`, which asks a small local
   model to write one short imperative lesson from the (task, response) pair,
   embeds it, and skips it if it's a near-duplicate (cosine ≥ `0.92`) of an
   existing lesson. Otherwise it's stored and becomes retrievable for future
   tasks.

The whole memory layer is one SQLite file, `memory.db` (gitignored — it's
local state, not source), with an `fts5` virtual table for the lexical half
and a `BLOB` column for embedding vectors on the semantic half.

## Interfaces

### MCP tools (for Claude Code / any MCP client)

Registered by `server.py` as the `local-llm` MCP server:

| tool | purpose |
|---|---|
| `trilobite(prompt, trace=False, strict=None, ...)` | Interactive front door to the full learning loop: retrieve → augment → generate → capture. Always uses the local coder model/alias. Returns the answer with a trailing `[interaction_id: <id>]` footer. |
| `offload(prompt, tier="fast", learn=True, ...)` | The general-purpose offload tool for fleets/subtasks. Only the local `code` tier with `learn=True` (default) takes the learning path (retrieval + capture + footer); `fast`/`general`/cloud tiers or `learn=False` just call the model plainly, no memory involved. |
| `record_outcome(interaction_id, signal)` | Feed a real outcome back in. `signal` ∈ `tests_passed`, `accepted`, `compiled`, `rejected`, `failed`. Good outcomes trigger lesson distillation. |
| `trilobite_stats()` | Read-only: interaction/outcome counts, outcome breakdown by signal, most recent lessons. Works even if Ollama is down (reads only `memory.db`). |
| `status()` / `unload()` | Bridge-level: what's installed / in VRAM, and force-unload a tier. |

Two flags worth knowing on `trilobite(...)`:
- **`trace=True`** — the model is asked to externalize its reasoning
  (`## Reasoning` then `## Answer`), and the response is appended with a
  `TRACE` block showing the *system's* actual decision context: which
  lessons were retrieved, the exact augmented prompt sent to the model, which
  model/tier was used, and the generation params. Useful for debugging why an
  answer looked the way it did.
- **`strict=True`** (or env `TRILOBITE_STRICT=1`) — pins the call to the
  fine-tuned `trilobite` Ollama alias only, and errors out instead of
  silently falling back to the base coder model if that alias isn't
  installed. Default behavior falls back silently.

### `trilobite` terminal REPL

`trilobite_repl.py` — an interactive terminal session against the real loop
(same code path the MCP tools use), with slash commands:

```
/help              show help
/trace [on|off]    toggle trace mode (bare = on)
/strict [on|off]   toggle strict mode (bare = on)
/stats             show trilobite's learning stats
/lessons           show the 10 most recent distilled lessons
/pass, /good       record the last answer as tests_passed
/fail, /bad        record the last answer as failed
/run               actually execute the code block from the last response
/train [N]         grounded self-learning: practice N tasks (default 3, max 10)
/exit, /quit, /q   leave
```

Plain English also works for the toggles/actions above (a conservative
classifier in `intents.py` only fires on short, control-like turns, so real
coding questions are never hijacked) — e.g. *"strict on, show your
reasoning"*, *"run it"*, *"train yourself"*.

### OpenAI-compatible proxy

`trilobite_serve.py` runs an HTTP server (stdlib `http.server`, zero deps)
speaking the OpenAI chat-completions API (`/v1/chat/completions`,
`/v1/models`, streaming supported) in front of the *real* trilobite loop —
not raw Ollama. So any OpenAI-compatible chat UI (e.g. Open WebUI) gets the
same lesson retrieval, capture, and slash-command powers (`/stats`, `/pass`,
`/fail`, `/trace`, `/strict`, `/run`, `/train`) as the REPL, just sent as
chat messages.

```
./venv/Scripts/python.exe trilobite_serve.py [port]   # default port 11435
# point your chat UI's OpenAI API base at http://127.0.0.1:<port>/v1 (any api key)
```

## Install / run

### Quick server setup (just the model, on a remote/fresh box)

```bash
git clone <this-repo-url>
cd <repo>
bash deploy_trilobite.sh
```

This installs Ollama if missing, picks a Qwen2.5-Coder size that fits the
box's RAM (7B / 3B / 1.5B), pulls it plus `nomic-embed-text`, and creates the
self-aware `trilobite` Ollama alias (a Modelfile `FROM` the base model with a
system prompt describing what trilobite actually is). That gets you `ollama
run trilobite` and the raw HTTP API. It does **not** install the Python
learning loop (retrieval/capture/`/train`/trace/the proxy) — copy this repo
over and follow local dev setup below for the full system.

### Local dev (Windows, this repo)

```bash
cd "~/Trilobite-model"

# Python deps (venv already present in this repo)
./venv/Scripts/python.exe -m pip install -r requirements-dev.txt
./venv/Scripts/python.exe -m pip install mcp

# one-time: pull the embed model + create the 'trilobite' Ollama alias
./venv/Scripts/python.exe setup_alias.py

# run the test suite
./venv/Scripts/python.exe -m pytest -q

# interactive REPL
./venv/Scripts/python.exe trilobite_repl.py
# or, on Windows, the .cmd wrapper (starts Ollama + creates the alias if needed):
trilobite.cmd

# OpenAI-compatible proxy
trilobite-serve.cmd
```

As an MCP server for Claude Code, `server.py` is registered at user scope
(`~/.claude.json`) and runs from `./venv`; restart Claude Code to pick up
tool changes.

### Tuning (env vars)

| var | default | purpose |
|---|---|---|
| `LOCAL_LLM_KEEP_ALIVE` | `2m` | how long a model lingers in VRAM after use |
| `LOCAL_LLM_FAST` / `_CODE` / `_GENERAL` | `qwen2.5:3b` / `qwen2.5-coder:7b` / `qwen2.5:7b-instruct` | swap the model per tier |
| `LOCAL_LLM_TIMEOUT` | `300` (s) | Ollama request timeout |
| `TRILOBITE_STRICT` | unset | default `strict` behavior for `trilobite(...)` |
| `TRILOBITE_MIN_SIM` | `0.65` | retrieval relevance threshold (see [How the loop works](#how-the-loop-works)) |
| `TRILOBITE_EMBED_MODEL` | `nomic-embed-text` | embedding model for retrieval |

## Self-training

`/train N` (REPL, proxy, or the underlying `training_tasks` pool) samples N
small, self-contained coding tasks (currently ~70, e.g. `reverse_string`,
`is_prime`, `LRUCache`, `topological_sort`) — each with an assert-based
check. For each task: ask trilobite → extract the fenced code block
(`grounding.py`) → actually run it in a subprocess against the check → record
`tests_passed` or `failed` via `record_outcome`. Passing runs can grow the
lesson store; this is how the "improves over time" claim is meant to become
real, driven entirely by grounded execution rather than the model's own
say-so.

`eval_retrieval.py` is the honesty check on top of that: it runs a
**held-out** set of tasks (disjoint by name from the training pool — enforced
by a test) under two conditions on the *same* model — retrieval **on**
(`server.trilobite`, real loop) vs. baseline **off** (same model, no lesson
injection, no capture) — and grounds pass/fail the same way. The tool is
chunk-resumable (`python eval_retrieval.py [start] [count]`) so it can run in
short foreground pieces.

The honest finding from that harness: retrieved lessons only help if they're
actually **relevant**. An earlier run showed irrelevant lessons being
injected and hurting pass rate; that's what motivated the relevance threshold
(`min_sim`) in `retriever.py` — the goal past that fix is closer to "do no
harm" than "guaranteed uplift" on easy tasks. See [Status & honest
caveats](#status--honest-caveats).

## Weight training (QLoRA)

Everything above is inference-time memory (retrieval), not weight changes.
There's a separate, optional pipeline to actually fine-tune the model on
good-outcome (task, response) pairs — full details in
[`TRAINING.md`](TRAINING.md). Honest summary:

- **Prepared but never run end-to-end.** `export_training_data.py` →
  `qlora_train.py` (QLoRA via `bitsandbytes` + `peft`) has been validated to
  parse and exercise the pipeline shape, not to actually complete a training
  run and produce a working adapter.
- **The dataset is tiny** — currently ~60 examples in `training_data.jsonl`,
  enough to prove the pipeline runs, not enough to expect a real behavior
  change. Hundreds, not tens, is the realistic bar.
- **A 7B model on a 6 GB GPU is marginal** — the default trains a 1.5B base
  instead (`Qwen/Qwen2.5-Coder-1.5B-Instruct`) as the path most likely to
  actually complete without OOM; 7B needs WSL2 or a bigger GPU.
- Registering a trained LoRA adapter back into Ollama requires converting it
  to GGUF, which is explicitly flagged in `TRAINING.md` as the least-tested
  part of the whole guide.

Do not run `qlora_train.py` unattended (no fleets/cron/CI) — watch VRAM and
be ready to interrupt on OOM.

## Architecture

```
server.py            MCP tools (trilobite, offload, record_outcome, trilobite_stats, status, unload)
trilobite_repl.py     interactive terminal REPL over the same loop
trilobite_serve.py    OpenAI-compatible HTTP proxy over the same loop
orchestrator.py       retrieve -> augment -> generate -> capture (the pure loop function)
retriever.py          hybrid lexical (FTS5) + semantic (embeddings) retrieval, RRF fusion, relevance threshold
memory_store.py       SQLite schema + CRUD (interactions, outcomes, lessons, lessons_fts)
embeddings.py         thin wrapper over a local Ollama embed model + cosine/blob helpers
reward.py             outcome signal -> scalar reward, "is this good enough to learn from"
reflection.py         distill a lesson from a good outcome, dedup by embedding similarity
intents.py            conservative NL classifier for slash-command equivalents (REPL + proxy)
grounding.py          extract a fenced code block and actually execute it in a subprocess
training_tasks.py     curated pool of ~70 grounded practice tasks for /train
eval_retrieval.py     retrieval-vs-baseline eval on a disjoint held-out task set
export_training_data.py / qlora_train.py   optional QLoRA fine-tune pipeline (see TRAINING.md)
setup_alias.py         one-time: pull embed model, create the 'trilobite' Ollama alias
deploy_trilobite.sh    stand up just the Ollama model + alias on a fresh/remote box
```

## Status & honest caveats

- **89 unit tests, all passing** (`./venv/Scripts/python.exe -m pytest -q`).
  They cover the memory store, retriever, reward/reflection logic, grounding,
  intents, training task pool shape, and the eval harness's own invariants
  (e.g. held-out/training disjointness) — not end-to-end model quality.
- **Retrieval's real-world benefit is unproven, especially on easy tasks.**
  The relevance-threshold fix (`min_sim`) is a *do-no-harm* correction, not
  evidence of measured uplift — `eval_retrieval.py` exists specifically so
  this claim can be checked with real numbers over time instead of asserted.
- **Local-only by design.** Local tiers and the whole learning loop run on
  your own GPU; nothing captured or learned ever touches the network. Cloud
  tiers exist for convenience on non-private work and are deliberately kept
  out of the learning loop.
- **Weight fine-tuning is unrun.** See [Weight training
  (QLoRA)](#weight-training-qlora) — the pipeline exists and is tested to
  parse, but has never produced a working adapter.
- **This is a hobby/research project**, built with heavy AI assistance
  (Claude Code). Expect rough edges; read the code before trusting it with
  anything that matters.
