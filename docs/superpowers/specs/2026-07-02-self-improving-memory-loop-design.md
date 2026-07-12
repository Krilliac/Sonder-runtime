# Self-Improving Local Coding Assistant — Slice #1: Memory + Reward Loop

**Date:** 2026-07-02
**Status:** Approved design, pre-implementation
**Home:** `~/.claude/mcp-servers/sonder-runtime/` (extends the existing `sonder-runtime` MCP server)

## North Star (context, not this slice's scope)

Sonder Runtime, a local coding-assistant orchestrator around Ollama and the
`sonder-runtime` MCP bridge, gets measurably better at *the user's* code over time without
manual babysitting. Decomposed into four sub-projects, built in dependency order:

1. **Experience/memory loop + reward capture** ← THIS SPEC
2. (folded into #1) Reward/signal harvester — execution-grounded quality signal
3. Periodic QLoRA fine-tune loop (adapter weights change; deployment uses a
   validated Ollama-compatible conversion or merged model)
4. Cambrian-style evolution of the prompt-assembly / routing *policy* (no weight training)

#3 and #4 are impossible without #1+#2; #1 alone delivers ~70% of the felt
"it's learning my codebase" benefit with zero training. Hence this slice first.

## Architecture Decisions (locked)

- **Serving layer:** Ollama, for everything through sub-project #3. Already wired.
  Ollama loads and serves the selected base, converted adapter, or merged model;
  it does not perform training. A trained adapter is deployed only after exact
  base-model matching, supported conversion/merge, and inference validation.
  `llama-server` is a later escape hatch for sub-project #4 only (decode-level
  control), not needed now.
- **Sonder Runtime is the orchestration system, not a foundation model.** The
  selected inference weights are frozen while serving. All learning in this
  slice lives in the orchestrator around that endpoint; explicit QLoRA/LoRA
  training is a separate workflow that updates adapter weights.
- **Entry points (mode A), two front doors onto the same loop:**
  - **Fleet/offload traffic** — the existing `offload` tool is transparently
    wrapped: every offloaded coding subtask is memory-augmented, captured, and
    later scored. No new habit for fleets.
  - **General interactive sessions** — a first-class `sonder` MCP tool
    (`mcp__sonder-runtime__sonder`) that a normal (non-fleet) session references
    directly ("use Sonder"). Same loop, interactive defaults.
  Both learn only from work that flows through them — an accepted limitation of
  this slice.
- **Named identity vs. learning wrapper (important distinction):** Ollama serves
  inference artifacts, while the memory/reward behavior lives in the Sonder
  Runtime orchestrator. We register an Ollama alias `sonder` (Modelfile `FROM`
  the code-tier base) as the stable rollback identity. Validated personal
  training artifacts are published separately as `sonder-personal:latest` and
  promoted by runtime policy only after validation; `sonder:latest` remains
  available for rollback.
- **Privacy:** all local tiers. Embeddings via a local Ollama embed model. Private
  code never leaves the box. No cloud tier touches captured data.

## Where the code lives

Extend the existing `sonder-runtime` MCP server (`server.py`) in place rather than
building a separate proxy. The `offload` tool is the single choke point all
traffic already flows through, so retrieval-injection + capture wrap it there.
New sibling tools are added to the same FastMCP instance. One process, one store.

New modules (each independently testable, kept small and focused):

- `memory_store.py` — SQLite schema + CRUD. No ORM.
- `retriever.py` — hybrid lexical + semantic retrieval with rank fusion.
- `embeddings.py` — thin wrapper over the local Ollama embed model.
- `reward.py` — outcome→scalar scoring + `record_outcome` logic.
- `reflection.py` — distill a "lesson" from good outcomes.
- `server.py` — wires the above into `offload` and registers new tools.

## Components

### Memory store (`memory_store.py`)
A single SQLite file (`memory.db`, gitignored). Uses two stdlib-`sqlite3`
features — no extra native deps for the lexical half:

- `interactions(id TEXT PK, task, retrieved_ctx, response, tier, ts)`
- `interactions_fts` — FTS5 virtual table mirroring `task` (+ response) for lexical search
- `outcomes(interaction_id, signal TEXT, reward REAL, ts)`
- `lessons(id, text, embedding BLOB, source_interaction, ts)` — distilled memories
- `lesson_embeddings` handling: store the float vector as a BLOB; semantic search
  loads candidate vectors and cosine-ranks in Python (dataset is small; no FAISS
  needed for slice #1 — revisit only if the store grows past ~10k lessons).

### Retriever (`retriever.py`) — hybrid
- **Lexical:** FTS5 `MATCH` over `lessons`/past tasks → ranked list.
- **Semantic:** embed the incoming task, cosine-rank stored lesson embeddings.
- **Fusion:** Reciprocal-Rank Fusion (RRF) of the two ranked lists → top-k.
  RRF chosen over score-normalization because it's robust to the two subsystems'
  incomparable score scales and needs no tuning.
- Returns top-k lesson texts to prepend as context.

### Embeddings (`embeddings.py`)
Calls Ollama `/api/embeddings` with `nomic-embed-text` (pulled once, approved).
Local, private. Same stdlib-`urllib` pattern already in `server.py`. Fails soft:
if the embed model/endpoint is unavailable, retrieval degrades to lexical-only
rather than erroring the offload.

### Capture (in `offload`)
Wrap the existing `offload` body:
1. Before calling Ollama: `retriever.retrieve(prompt)` → prepend memories to the
   system/context (clearly delimited so they don't corrupt the task).
2. After: generate a short `interaction_id`, log the interaction row (+ FTS row).
3. **Return contract:** `offload` still returns the model's **text** (backward
   compatible with every existing caller/fleet), with a single trailing footer
   line appended: `\n\n[interaction_id: <id>]`. Callers that care about outcomes
   parse the id from the footer; callers that don't simply ignore it. This is
   chosen over returning JSON (which would break all current callers) and over a
   separate `last_id()` tool (racy under concurrent fleet use — the id must travel
   with the exact response the agent is holding).
4. Learning is gated by a new `learn: bool = True` arg so a caller can opt out
   (e.g. throwaway reformatting) and get the old pure-text behavior.

### General-session entry point (`sonder` tool + Ollama alias)
A new MCP tool `sonder(prompt, tier="code", system="", temperature=0.2,
num_predict=1024, num_ctx=4096) -> str` — the front door for normal interactive
sessions. It is a thin ergonomic wrapper over the same capture path used by the
wrapped `offload`:
- Defaults to the `code` tier (interactive coding help), always `learn=True`.
- Runs retrieval → Ollama → capture, returns the answer + `[interaction_id: <id>]`
  footer, so the session can `record_outcome` on it exactly like fleet traffic.
- Targets the Ollama alias `sonder` rather than a raw tier model name.

**Ollama alias `sonder`** — created once via a Modelfile
(`FROM qwen2.5-coder:7b`, carrying default system/params). Purpose is a *stable
rollback identity*, not new behavior: it appears in `ollama list` and `status`
and gives the user a name to reference. Sub-project #3 publishes validated
personal artifacts under a distinct name rather than replacing this alias. A
tiny setup step (`setup_alias.py` or a documented `ollama create` command)
creates it; the tool degrades to the raw `code` model if the alias is absent, so
the loop never hard-depends on it.

### Reward harvester (`reward.py` + `record_outcome` tool)
New MCP tool: `record_outcome(interaction_id: str, signal: str) -> str`.
- `signal ∈ {compiled, tests_passed, accepted, rejected, failed}` → scalar reward
  (execution-grounded signals weighted highest; the compiler is ground truth).
- Writes an `outcomes` row. Fleets/agents call this after their existing
  compile/test steps — no build-log scraping in this slice (explicit is
  unambiguous; auto-inference is a later refinement).

### Reflection (`reflection.py`)
On a *good* outcome (reward above a threshold), a cheap `fast`-tier `offload`
call distills a one-line lesson ("when X, do Y; pitfall Z") from
(task, response, signal). Store it in `lessons` with its embedding so future
retrievals surface it. This is the component that makes the system feel like it
is learning. Bounded: one lesson per good outcome, deduped by embedding similarity
against recent lessons to avoid flooding the store with near-duplicates.

## Data flow

```
offload(task, learn=True)
  -> retriever.retrieve(task)         # FTS5 + embeddings -> RRF -> top-k lessons
  -> prepend lessons to context
  -> Ollama (frozen GGUF)             # unchanged call path
  -> log interaction, mint id
  -> return response + "\n\n[interaction_id: <id>]"
        ... later, same workflow ...
record_outcome(<id>, "tests_passed")
  -> reward.score() -> write outcome
  -> if good: reflection.distill() -> lessons(+embedding)
```

## Explicitly OUT of scope (YAGNI)

No fine-tuning / weight changes; no Cambrian evolution; no `llama-server`; no
editor integration; no automatic reward inference from build logs; no FAISS/vector
extension; no cloud tiers in the learning path. These are sub-projects #3/#4,
gated on this slice proving useful.

## Testing

Deterministic unit tests per module, no live-Ollama dependency in core tests
(embed + generate calls mocked/stubbed):

- `memory_store`: insert → read back; FTS row stays in sync with base row.
- `retriever`: seed known lessons → assert RRF fusion ordering for crafted
  lexical-vs-semantic cases; assert lexical-only fallback when embeddings stubbed off.
- `offload` capture: asserts a row is written and the returned text ends with a
  parseable `[interaction_id: ...]` footer; `learn=False` writes nothing and omits footer.
- `sonder` tool: shares the capture path — same footer/row assertions as
  wrapped `offload`; asserts it falls back to the raw `code` model when the
  `sonder` Ollama alias is absent.
- `record_outcome`: updates reward; unknown id handled gracefully.
- `reflection`: with a stubbed model call, a good outcome writes exactly one
  deduped lesson; a bad outcome writes none.

## Risks / open notes

- **VRAM:** embed model + a 7B coder both touching the 6 GB card. `nomic-embed-text`
  is tiny (~0.3 GB) and short-lived; keep_alive already frees the coder. Monitor,
  but not expected to thrash.
- **Store growth:** Python-side cosine over all lessons is O(n) per query — fine to
  ~10k lessons; add an ANN index only if that ceiling is hit.
- **Footer leakage:** the `[interaction_id: ...]` footer could confuse a caller that
  treats the whole return as code. Mitigated by the `learn=False` opt-out and by
  putting the footer on its own trailing line after a blank line.
