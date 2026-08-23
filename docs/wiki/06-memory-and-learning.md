# Memory & Learning

Sonder's differentiator: it learns *your* context from grounded outcomes.
All memory is local SQLite in `memory.db`, owned by `memory_store.py`.

## What is stored

| Kind | Description |
|---|---|
| Conversation turns | Per-session user/assistant turns, with running summaries of overflow. |
| Facts | Durable, project-scoped statements you tell it to remember. |
| Lessons | Short, reusable takeaways distilled from *successful* interactions. |
| Interactions | Captured task→response rows with embeddings and token accounting. |
| Outcomes | Grounded signals (compiled, tests_passed, used, rejected, failed). |

## The learning loop

1. A coding interaction is **captured** (task, response, tier, project,
   embedding).
2. You (or an automated check) **record an outcome**: `/pass`, `/fail`, or
   `record_outcome(iid, signal)`.
3. The outcome is **priced** into a scalar reward
   ([domain/memory/rules.py](../../sonder_runtime/domain/memory/rules.py)):
   `tests_passed 1.0`, `used 0.9`, `accepted 0.8`, `compiled 0.7`,
   `rejected -0.5`, `failed -1.0`. The "good" threshold is `0.71` — above
   `compiled`, deliberately, so only verified-correct work distills.
4. A good outcome **distills a lesson** (deduped, FTS + vector indexed).
5. Next time, similar tasks **retrieve** those lessons and prior good
   solutions into the prompt.

Because the bar is real outcomes, over time it learns your patterns rather
than the internet's average. Cloud/teacher tiers are captured too, so a
frontier model's grounded wins become lessons and fine-tuning data for the
local model.

## Retrieval (recall)

`recall.py` finds prior **good-outcome** interactions whose task is
semantically close to the current one (cosine over embeddings, floor
`DEFAULT_RECALL_MIN_SIM = 0.72`), and injects a bounded, project-scoped set
into the prompt. Lessons are retrieved via FTS + vector search, then
**MMR-reranked** ([domain/memory/rules.py](../../sonder_runtime/domain/memory/rules.py))
to suppress near-duplicate lessons crowding out diverse ones.

Recall is **project-local by default**: `project=None` selects only
unscoped rows; cross-project recall requires an explicit override. This is
a privacy boundary, not just a filter.

## Sessions

`session` scopes conversation history; `project` scopes durable facts and
recall. Over the OpenAI HTTP endpoint, a full UI owns history; a thin
client naming a session gets server-side history rebuilt
([HTTP & Lifecycle](05-http-api-and-lifecycle.md)). Overflow turns are
summarized incrementally through the model gateway.

## Facts

```
sonder_remember_fact("The project codename is HELIOS.", project="demo")
```

Stored durably and injected into project-scoped prompts. In the live A/B
runs, a scoped fact was recalled exactly ("HELIOS") where a bare model
invented an answer.

Re-asserting a fact the project already holds (same statement up to
whitespace/case) is refused with the existing fact's id rather than stored
twice — facts are injected into every prompt for the project, so a duplicate
would spend prompt budget on a repeat forever. The duplicate check never
looks across projects: project scope is a privacy boundary, and a match
against another project's facts would leak them.

## Observability & hygiene

- `/stats` — lessons, interactions, outcomes, token ledgers by tier.
- `memory_search`, `memory_export`, `session_export` — inspect local memory.
- `learning_health_status` — outcome coverage, signal quality, distillation
  yield, the evidence-level findings above, and the attribution lane's own
  session counters (`grounded_outcomes`: noted / attributed / self-blocked /
  unmeasured / write-failed — process-local, so a fresh server reports zeros
  while the store keeps every outcome ever written).
- `memory_quality_report`/`_repair` — audit and dry-run/prune duplicate
  lessons. The audit also reports evidence-level findings, all read-only and
  fail-closed on missing provenance (no stored embedding or no scored outcome
  means no claim): **conflicting lesson pairs** (similar embeddings, opposite
  grounded outcomes, caller judgement deciding polarity over the runtime's
  own grades), **stale lessons** (positive evidence at least two half-lives
  old whose age-decayed effective score fell below a floor — experimental,
  diagnostics only), and **duplicate facts** (per-project only; removal stays
  with `sonder_forget_fact`).
- `memory_privacy_review`/`_repair` — redacted privacy findings and removal.
- `memory_embedding_backfill` — refresh stale/missing vectors.

## Pure rules vs. storage (SPEC-3)

Scoring, the recall threshold, and MMR selection are pure functions in
`sonder_runtime/domain/memory/rules.py` (similarity injected, no embedding
adapter). `sonder_runtime.adapters.memory_rerank` and `recall.py` delegate to
them, while reward callers use the domain rules directly —
one definition, unchanged behavior. See
[Package Architecture](14-package-architecture.md).
