# Sonder Runtime conversation memory + memory subsystem — design

Date: 2026-07-05
Status: implemented (all 5 phases; 225 tests green; live 2-turn recall verified)
Repo: `~/.claude/mcp-servers/sonder-runtime`
Memory default: **ON**. A call with no `session` threads the shared `default`
session; pass `session="none"` for a one-off single turn, or a distinct id to isolate
a thread. Same convention for `project` facts. (Chosen over the original default-off
proposal per user direction — the goal is that follow-ups "just work".)

## Problem

Sonder Runtime forgets follow-ups. Every entry surface sends the model only
`[system, current_prompt]` — no prior turns:

- **MCP `sonder` tool** (`server.py`) — single `prompt`, single turn.
- **REPL** (`sonder_repl.py:233`) — calls `server.sonder(line)` fresh per line.
- **HTTP serve** (`sonder_serve.py:345`) — `_last_user_message(messages)` deliberately
  discards everything the chat UI sends except the final user turn.

The existing "memory" is the *lessons* learning loop (distilled tips retrieved by
FTS+embedding), not conversation continuity. This spec adds a **conversation layer**
plus three further memory capabilities, all orthogonal to and non-breaking for the
lessons/reward loop.

### Why not "just use a giant / 1M context"

At the time of this design, Sonder Runtime's selected local inference base was
Qwen2.5-Coder 7B, served by Ollama. Sonder Runtime itself is the orchestration
system, not those weights. That base has a 32K native context (~128K with YaRN),
and KV-cache cost is ~56 KB/token for this architecture, so on the 6 GB RTX 4050
(model itself ~4.7 GB) the practical local ceiling is ~16–32K tokens; 1M would
need ~56 GB (fp16) / ~14 GB (4-bit KV) and is not feasible locally. The correct
lever is *carrying the right few turns* (threading) and *compressing the rest*
(summarization), not enlarging the window. A metered hosted tier may expose a
longer context, but Sonder Runtime uses it only after explicit hosted-tier opt-in;
it is not part of the local path.

## Goals

1. Follow-up turns reach the model, on every surface.
2. Threads are navigable and resumable across days.
3. Long threads don't overflow the local window (summarize, don't drop-and-forget).
4. Sonder Runtime can reuse whole past solutions, not just distilled lessons.
5. Sonder Runtime carries durable per-project facts it should always know.

## Non-goals

- `offload` stays single-turn — its contract is "fully self-contained subtask." Out of scope.
- No change to the lessons/reward/`record_outcome` loop semantics.
- No cloud routing. Local-only, as today.
- Auto-extraction of project facts from conversation — manual add for v1 (future work).

## Design overview

Five phases, each independently shippable and testable. All additions are **additive
and default-off**: with defaults unset, behavior is byte-for-byte identical to today.

### Data model changes (`memory_store.py`)

All via idempotent migrations (`ALTER TABLE … ADD COLUMN` guarded by a
`PRAGMA table_info` check; `CREATE TABLE IF NOT EXISTS`).

```
interactions:  + session_id     TEXT NULL      -- threads a turn to a conversation
               + task_embedding  BLOB NULL      -- for semantic recall (phase 4)

sessions (new):
    session_id        TEXT PRIMARY KEY
    title             TEXT                       -- auto-generated short title
    summary           TEXT                       -- rolling compression of old turns
    summarized_through TEXT                      -- last interaction id folded into summary
    project           TEXT NULL                  -- optional link to a facts project
    created_ts        TEXT DEFAULT CURRENT_TIMESTAMP
    updated_ts        TEXT

facts (new):
    id         TEXT PRIMARY KEY
    project    TEXT                              -- scope key (workdir path or name)
    text       TEXT
    embedding  BLOB NULL
    ts         TEXT DEFAULT CURRENT_TIMESTAMP
```

Old rows keep `session_id = NULL` → excluded from every session query → today's
single-turn path is untouched.

### Generation path (`server.py`)

`_make_generate` returns `gen(prompt, history=None)`. When `history` (a list of
`{role, content}`) is present, the message list becomes:

```
[system?] + history + [{"role": "user", "content": prompt}]
```

`orchestrator.run_with_learning(conn, task, tier, gen, history=None)` passes `history`
straight through to `gen`. Only the *current* turn is lesson-augmented and logged;
history turns are raw context. Env `SONDER_SESSION_NUM_CTX` (default **8192**)
applies to sessioned calls; env `SONDER_MAX_TURNS` (default **12**) caps live turns.

---

## Phase 1 — Conversation threading (foundation)

**memory_store**
- `session_history(conn, session_id, max_turns)` → oldest-first `[(task, response), …]`
  for that session, limited to the last `max_turns`.
- `log_interaction(..., session_id=None)` — persist the column.
- migration for `interactions.session_id` + `sessions` table.

**server.sonder** — new param `session: str = ""`. When set:
1. `history_pairs = session_history(session, SONDER_MAX_TURNS)`
2. build `history` messages (user=task, assistant=response) from the pairs,
3. generate with `num_ctx = SONDER_SESSION_NUM_CTX`,
4. log the new interaction with `session_id=session` (and upsert `sessions.updated_ts`).

**Surfaces**
- **REPL** — mint one `session_id` at startup, pass on every `server.sonder` call;
  `/new` starts a fresh session. `/train` keeps using session-less calls.
- **serve** — build `history` from the UI's own `messages` array (all turns before the
  final user message) and thread it; stop discarding it. The UI owns context here, so
  serve does **not** use the DB session store.
- **MCP tool** — I (the caller) pass a stable `session` id per conversation; persists
  across separate calls and across restarts via the DB.

**Tests** — history ordering + cap; orchestrator threads history into the message list;
server session round-trip (turn N sees turn N-1); serve builds history from a UI
`messages` array; regression: `session=""` still sends exactly one turn.

## Phase 2 — Rolling summarization

When a session's turn count exceeds `SONDER_MAX_TURNS`, fold the overflow (turns
older than the live window) into `sessions.summary` via the `fast` tier, incrementally:
`new_summary = summarize(old_summary + newly-overflowed turns)`, advancing
`summarized_through` so each turn is summarized once. History sent to the model becomes:

```
[{"role":"system","content":"Earlier in this conversation: <summary>"}] + last N turns
```

- **memory_store** — `get_session(conn, session_id)`, `update_session_summary(conn, session_id, summary, summarized_through)`.
- **summarizer** (new small module, mirrors `reflection.distill`) — `summarize(old_summary, turns, offload_fn)` using `fast` tier, low temperature, bounded `num_predict`.

**Tests** — overflow triggers summarization; `summarized_through` prevents re-folding;
summary is injected ahead of live turns; empty/short sessions never summarize.

## Phase 3 — Resumable / named sessions

- Auto-title a session from its first prompt via the `fast` tier (short, ≤6 words);
  fall back to a truncated first prompt if the model call fails.
- **memory_store** — `list_sessions(conn, limit)` → `[(session_id, title, updated_ts, turn_count)]`.
- **REPL** — `/sessions` (list), `/resume <id-or-title-prefix>` (switch), `/new` (fresh).
- **MCP** — new read-only tool `sonder_sessions()` (mirrors `sonder_stats`) so the
  caller can discover and resume threads by id.

**Tests** — titling from first prompt (+ fallback on model failure); list ordering by
recency with turn counts; resume by id and by title prefix.

## Phase 4 — Semantic recall of past solutions

Reuse whole prior solutions, not just distilled lessons.

- On `log_interaction`, compute and store `task_embedding = embeddings.embed(task)`
  (soft-fails to NULL, exactly like lessons).
- **recall** (mirrors `retriever._semantic_rank` + `min_sim` threshold) —
  `recall(conn, task, k=2, min_sim=…)` returns the top-k past interactions whose task
  is semantically similar **and** that carry a good outcome (join `outcomes`), each
  truncated. Reuses `SONDER_MIN_SIM` threshold semantics.
- **orchestrator.build_prompt** — add a bounded "Similar past solved task" block
  alongside the existing lessons block (both optional, both capped).

**Tests** — recall returns only good-outcome similar tasks above threshold; recall
block is bounded/truncated; no embeddings → soft-fail to no recall (no crash); recall
excludes the current session's own in-flight turn.

## Phase 5 — Project facts

Durable per-project facts Sonder Runtime always knows (toolchain, conventions, key paths) —
injected like lessons but **not** outcome-gated.

- **facts** table (above). `add_fact(conn, project, text)` embeds + stores;
  `facts_for_project(conn, project, task, k)` returns all facts for a small set, or
  semantic top-k when many.
- **server.sonder** — new param `project: str = ""`; when set, facts for that
  project are injected into the augmented prompt (own labeled block).
- **sessions.project** links a session to a project so resuming reloads its facts.
- **management** — MCP tool `sonder_remember_fact(project, text)`; REPL `/fact <text>`
  and `/facts` (uses the REPL's active project, settable via `/project <name>`).

**Tests** — facts injected for the active project only; add/list round-trip;
project="" injects nothing (regression); many-facts path uses semantic top-k.

## Backward compatibility

New params (`session`, `project`) default to `""`, which resolves to the shared
`default` session/project (memory ON). `session="none"` / `project="none"` opt out to
today's single-turn, fact-less behavior. Migrations are idempotent and additive; old
`interactions` rows have `session_id = NULL` and are excluded from all session/recall
queries. The lessons/reward/`record_outcome`/`sonder_stats` paths are unchanged.
`offload` is untouched (still single-turn, self-contained). REPL mints a fresh thread
per launch; `/train` runs with `session="none"` so practice never pollutes a chat.

## Rollout

Ship phase-by-phase in order (1→5); each phase is independently valuable and leaves the
tool fully working. Phase 1 alone fixes the reported "forgets follow-ups" bug.
