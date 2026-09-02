---
name: sonder-memory-and-training
description: >-
  Operate and modify Sonder Runtime's learning loop end to end: lesson memory,
  calibrated retrieval thresholds, outcome recording, and the attended QLoRA
  training/deployment lifecycle. TRIGGER when the user says "lesson memory",
  "retrieval threshold", "record_outcome", "quarantine a lesson", "train the
  adapter", "QLoRA", "promotion", or "rollback the model". DO NOT TRIGGER for
  privacy-classifier internals, secret scrubbing rules, or what counts as
  private text — that boundary belongs to sonder-security-and-privacy; for
  test-gate policy and promotion-suite QA detail use sonder-validation-and-qa.
---

# Sonder Runtime: memory, learning loop, and attended training

Sonder Runtime learns locally in four stages: grounded outcomes attach real
verdicts to model generations, good outcomes distill into one-sentence
**lessons**, hybrid retrieval injects relevant lessons into future prompts, and
(optionally, attended-only) the proven interactions become QLoRA fine-tuning
data for a personal adapter. Every threshold below is **calibrated from
measured data, not chosen by taste** — each one records where its number came
from, and this skill tells you how to re-derive it when the corpus changes.

Vocabulary used throughout:

- **Lesson** — one imperative sentence distilled from a solved (or failed)
  task, stored with an embedding in the `lessons` table of `memory.db`.
- **Interaction** — one captured prompt/response pair, identified by the
  `[interaction_id: <id>]` footer on learn-enabled responses.
- **Outcome / signal** — a named verdict (`tests_passed`, `accepted`, ...)
  recorded against an interaction; priced into a reward.
- **Quarantine** — evidence-gated exclusion of a lesson from retrieval.
- **QLoRA** — LoRA fine-tuning over a 4-bit (NF4) frozen base model.

## Where the data lives

| Store | Location | Source |
|---|---|---|
| `memory.db` | Sonder state home (`%LOCALAPPDATA%\sonder` on Windows); `SONDER_DB` env overrides | `sonder_runtime/platform/paths.py:201` |
| `embed-cache.db` | state home; `SONDER_EMBED_CACHE_DB` overrides | `sonder_runtime/adapters/embedding_cache.py:57` |
| Training exports | `training_data.jsonl` + `.manifest.json` (repo root or run dir) | `export_training_data.py:301` |
| Adapters | `sonder-personal-lora/` (gitignored) | `qlora_train.py:51` |

A legacy repo-root `memory.db` is migrated once into the state home under a
cross-process lock, staging `-wal`/`-shm` companions and publishing via
`os.replace` (`paths.py:274-313`). **Never commit `memory.db` or any training
JSONL** — they contain raw prompts and responses. `.gitignore` lines 8-10 and
21-24 cover `memory.db*`, `training_data.jsonl`, `personal_dataset.jsonl`,
`sonder-personal-lora/`, `combined_personal.jsonl`, and `Modelfile.personal`.
Personal adapters were once tracked and were deliberately untracked in commits
`8af510bd` and `9bd87c72`; do not recommit them. What counts as private text
inside a lesson or export is `contribute.private_reasons` — its rule set is
sonder-security-and-privacy territory.

## Retrieval: calibrated thresholds with provenance

Entry point: `retriever.retrieve_with_ids(conn, task, k=5, ...)`
(`retriever.py:559`). The pipeline, in order:

1. Load usage stats enriched with blame-adjusted loss attribution
   (`usage_stats_with_attribution`, `retriever.py:357`).
2. Compute the quarantine exclusion set for this task (probation sampling
   included, `retriever.py:493`).
3. Over-fetch FTS5 lexical hits (`candidate_limit + len(quarantined)`) so
   quarantined rows ranked ahead by FTS cannot starve valid matches
   (`retriever.py:568-578`).
4. Embed the task; rank the corpus semantically, skipping stored vectors whose
   embedding model/revision/dimension differ from the query's — a vector from
   another embedding space is not evidence (`retriever.py:189-219`).
5. If embeddings are unavailable **or no compatible corpus vector exists**,
   soft-fail to lexical-only and require **two content-word anchors** per
   candidate instead of trusting one ambiguous token (`retriever.py:600-624`).
6. Fuse lexical + semantic ranks with RRF (k=60), add a bounded usage boost
   (cap ±0.01, confidence shrunk toward a pseudo-count-10 prior so unscored
   retrievals earn nothing, `retriever.py:506-544`).
7. Filter by the similarity gate, then MMR-select the final k for diversity
   (`retriever.py:632-646`). `SONDER_MMR_LAMBDA=1` restores pure relevance
   order; the default is 0.5 (`retriever.py:547-556`).

The design philosophy is explicit: **inject nothing when nothing fits.**
Irrelevant lessons in a prompt actively hurt, so every gate fails toward an
empty result rather than a plausible one.

### The thresholds and where their numbers came from

| Constant | Value | Provenance (all in `retriever.py`) |
|---|---|---|
| `DEFAULT_MIN_SIM` | 0.62 | Recalibrated 2026-07-06 on the 557-lesson corpus via `tune_min_sim.py` (nomic-embed-text). Positives: min 0.612 / median 0.728; negatives: max 0.611. 0.62 is the lowest zero-noise threshold — recall 0.95, noise 0.00, best Youden's J (lines 10-18) |
| `DEFAULT_UNCORROBORATED_MIN_SIM` | 0.70 | A reproduced cross-domain false positive at cosine 0.650211 on the live 979-lesson corpus. Semantic-only hits (fewer than 2 lexical anchors) must clear 0.70; anchored hits keep the 0.62 gate (lines 19-25) |
| `MIN_LEXICAL_ANCHORS` | 2 | Same incident; one anchor is an acronym collision (line 25) |
| `SONDER_MIN_SIM` env | overrides `DEFAULT_MIN_SIM` | line 564 |

The old 0.65 gate was tuned on the tiny game-ladder corpus and dropped genuine
0.60-0.65 hits (the sql-injection lesson scored 0.650) with no precision gain.
The lesson generalizes: **a threshold tuned on one corpus silently rots on the
next one.** After any large corpus change (roughly: hundreds of lessons added,
or the embedding model changes), recalibrate:

```powershell
.\venv\Scripts\python.exe tune_min_sim.py
```

It sweeps 0.50-0.70 against 22 positive coding intents and 15 off-domain noise
probes, prints recall/noise/J per threshold plus the lowest zero-noise
threshold, and refuses to run without a compatible embedded corpus
(`tune_min_sim.py:126-141`). Update `DEFAULT_MIN_SIM` and its provenance
comment together — a bare number with no derivation is how 0.65 went stale.

## Quarantine: the lesson jail

A lesson that keeps appearing in failing retrievals is excluded from
retrieval. Three gates must ALL agree (`retriever.py:390-471`):

| Gate | Constants (`retriever.py:30-99`) | Why this shape |
|---|---|---|
| Raw floor | `QUARANTINE_MIN_LOSSES=5` spanning `QUARANTINE_MIN_DISTINCT_TASKS=2`, or `QUARANTINE_REPEAT_TASK_MIN_LOSSES=6`; `QUARANTINE_MAX_AVG_REWARD=-0.5` | Conservative sample-size guard |
| Blame-adjusted volume | `QUARANTINE_MIN_ATTRIBUTABLE_LOSSES=2.0` **shares** | A failing task writes its reward onto EVERY retrieved lesson. Measured 2026-08-06: 493 loss rows trace to only 144 failing interactions (mean cohort 3.424). Blame is split 1/cohort per row; the gate counts shares, not rows (lines 36-53) |
| Reference class | `QUARANTINE_FREQUENCY_BANDS` + `QUARANTINE_BAND_ALPHA=0.01` | Loss rate is dominated by retrieval frequency, not quality (50.62% for once-retrieved lessons vs 1.24% at 100+ retrievals, measured over 9,256 scored retrievals). Each lesson's loss run must be improbable at p ≤ 0.01 for its OWN frequency band (lines 55-88) |

Recovery: quarantine blocks the very win that would clear it, so after
`QUARANTINE_PROBATION_AFTER_HOURS=24` the lesson is admitted on a
deterministic ~1-in-20 sample of tasks (`QUARANTINE_PROBATION_ONE_IN=20`,
keyed on lesson+task so reruns are reproducible); the
`QUARANTINE_COOLDOWN_HOURS` timer (written `24 * 7` in source — 7 days) lifts
it entirely (`retriever.py:34, 90-99, 478-490`). Share sums are compared with a 1e-9 tolerance
because 6 × (1/3) binary-sums to just under 2.0 (`retriever.py:422-428`).

When debugging "why was this lesson retrieved / not retrieved": call
`retriever.lesson_quarantine(stats)` — it returns the full auditable evidence
dict, including `attribution_source` (`measured` vs the deliberate
`unattributed` upper bound, `retriever.py:372-387`).

## Outcomes: the two-population rule

Signals and prices (`sonder_runtime/domain/memory/rules.py:18-32` — prices are
canonical and must never change once shipped, because the exporter detects
corruption by comparing stored rewards against them):

| Population | Signals (reward) | Who files it |
|---|---|---|
| Caller-judged | `used` (0.9), `copied` (0.85), `accepted` (0.8), `edited` (0.75), `rejected` (-0.5) | A human/agent reviewing delegated work |
| Machine-graded | `tests_passed` (1.0), `compiled` (0.7), `failed` (-1.0) | A runner reporting what the code did |

`GOOD_THRESHOLD=0.71` sits deliberately above `compiled`: building is not
passing, so a compile alone never distills a lesson or exports a training row.

**The populations are never averaged.** Measured on the live store, 9,049 of
9,450 outcome rows are `tests_passed` from the self-graded curriculum
(`server.py:5800-5802`); blending them with the ~190 caller judgements produces
a number that reads like accuracy and is not one. (A differently dated
in-source snapshot of the same store — ~9,200 rows, 8,883 `tests_passed` —
lives at `grounded_outcomes.py:3-6`; the two figures were taken at different
times, and `sonder-external-positioning` carries the same cross-note. Cite
either with its source, never a blend.)

Two entry points enforce this:

- `record_outcome(interaction_id, signal)` — the MCP tool (`server.py:5740`).
  Every row it writes is stamped `source='caller'`, and it accepts **no
  `source` argument on purpose**: provenance a caller can choose is provenance
  a caller can misstate (`server.py:5764-5770`). If YOU ran the tests, prefer
  `accepted`/`rejected` over `tests_passed`/`failed`.
- `record_self_graded_outcome` — the in-process twin (`server.py:5794`),
  stamped `OUTCOME_SOURCE_SELF_CURRICULUM`, used by `curriculum_run` and
  `game_ladder`. It is deliberately **not** an MCP tool, so a caller cannot
  relabel its own verdicts into the self-marked bucket.

### Automatic attribution (grounded outcomes)

`grounded_outcomes.py` removes the human from the report: when a verifier tool
(`test_run`, `build_run`, `lint_run`, `typecheck_run`, `run_code`, ... — full
map at `grounded_outcomes.py:58-70`) runs shortly after a generation, its
verdict is filed as that generation's outcome. Deliberate limits, all of which
fail toward recording NOTHING (wrong attribution poisons the very population
this exists to clean):

- `ATTRIBUTION_WINDOW_SECONDS=900`, `MAX_PENDING=64` (lines 50-53).
- One signal per generation per verification kind; a tool may never grade work
  it generated itself (`_candidate`, lines 364-389).
- Project and run-id only ever narrow a match, never widen one.
- `evaluation_infrastructure_error` / `code_runner_infrastructure_error`
  (lines 141-206) exist because `ok` alone cannot distinguish "the build
  failed" from "there was no build system" — both once arrived as `failed` at
  reward -1.0 against work nothing examined, and consumed the pending entry so
  the later genuine verdict had nothing to attach to.
- A failed database write refunds the consumed evidence (lines 467-476).

Diagnose the ledger with `grounded_outcomes.stats()`: `self_blocked` means the
self-grading guard worked; `unlinked` means nothing was waiting. They are
counted apart on purpose.

### Learn tiers and distillation

`SONDER_LEARN_TIERS` selects which offload tiers feed the loop; the default is
every configured local tier — `fast,code,general`
(`server.py:815-827`, base tier list at
`sonder_runtime/domain/runtime_policy/rules.py:19`). Cloud tiers are captured
too: a paid frontier model's grounded wins become lessons and fine-tuning data
that the local model retrieves later (`offload` docstring, `server.py:4715-4728`).

Distillation (`reflection.py`) turns a good outcome into a lesson:

- `DUP_THRESHOLD=0.92` cosine for semantic dedup; dedup is fail-closed on
  embedding provenance and also checks **tombstones**, so a pruned lesson
  cannot be semantically reintroduced (`reflection.py:100-180`).
- `DISTILL_SYSTEM` bans vague filler ("efficiently", "best practices", ...)
  and demands `NONE` otherwise; regexes require a concrete anchor (a dotted
  call, backticked code, CamelCase API, snake_case identifier, or big-O bound)
  before storing (`reflection.py:9-71`).
- A separate pitfall lane (`distill_pitfall`, `reflection.py:216`) extracts
  "avoid X, do Y" from failures, refusing example echoes, non-implementation
  restatements, and instance-specific call sequences (lines 249-321).
- The distillation model call runs under its own `SONDER_DISTILLATION_TIMEOUT`
  budget, default 20 s, bounded by the live ceiling — so `record_outcome`
  never inherits the 5-minute `SONDER_TIMEOUT=300` model ceiling
  (`sonder_runtime/domain/distillation_policy.py:18`, `server.py:319, 3260`).

## The attended training lifecycle

Everything below is QLoRA-only and **attended-only**: training is never
started by bootstrap, Autopilot, cron, or a fleet (`TRAINING.md:17-19`).
`adaptive_training.py` is stdlib-only — heavy ML deps stay isolated in
`qlora_train.py` — so `hardware`, `plan --dry-run`, and `status` work on any
install (`adaptive_training.py:1-5`).

```bash
python adaptive_training.py hardware
python adaptive_training.py plan --dry-run --model auto
python export_training_data.py
python adaptive_training.py start --confirm --model auto
python adaptive_training.py status
python adaptive_training.py deploy --llama-cpp /path/to/llama.cpp
python adaptive_training.py rollback
```

(The same verbs exist in the REPL as `/training ...`; `TRAINING.md:38-66`.)
The planner keeps VRAM and RAM as separate budgets; `--full-finetune` is a
feasibility report only, and the start path rejects dense training and CPU
offload (`TRAINING.md:29-34, 98-104`).

### Export (`export_training_data.py`)

`EXPORT_SCHEMA=1`; caps: 50,000 source interactions, 200,000 evidence rows
(lines 28-32). Eligibility is fail-closed: an interaction needs at least one
grounded good signal, ANY negative or corrupt-reward row vetoes it, and the
shared privacy classifier excludes flagged text (lines 47-217). Duplicate
prompts keep the strongest-then-newest response, ranking population before
price — ordering by reward alone let self-graded `tests_passed` (1.0) outrank
every caller judgement in a ~98% self-graded corpus (lines 108-126). Output is
written atomically with a manifest recording the dataset SHA-256 and only
aggregate rejection counts; the stale manifest is deleted **before** the data
commit so a failure can leave no manifest but never a false one (lines 329-339).

### Launch authorization (`start --confirm` → `qlora_train.py`)

`start` builds a run under `sonder-personal-lora/runs/<run-id>/`, freshly
exports the dataset into it, and issues a fresh one-use launch capability:
a random token whose SHA-256 lands in the plan manifest, plus an HMAC over
`plan_sha256` + `data_sha256` (`adaptive_training.py:458-481, 803-879`).
`qlora_train.authorize_launch` (`qlora_train.py:118-186`) then verifies, in
order: the pinned HF revision, an exclusive `.launch-claimed` file
(`O_CREAT|O_EXCL` — replay is rejected), the token digest, a ≤300 s manifest
age, schema 2, that the manifest lives inside its approved run directory, that
base/data/adapter/GPU match the controller plan, and the dataset SHA-256 via
full re-inspection. Direct invocation of `qlora_train.py` fails here, before
any heavyweight import. The authorized JSONL is all-or-nothing: malformed rows
abort the run rather than silently shrinking the approved corpus.

### Training constants (`qlora_train.py:42-84`)

| Item | Value |
|---|---|
| Base | `Qwen/Qwen2.5-Coder-1.5B-Instruct` default; 3B/7B mapped, each pinned to a reviewed 40-char HF commit (`HF_REVISIONS`, lines 43-47) |
| LoRA | r=16, alpha=32, dropout=0.05, attention+MLP projections |
| Schedule | batch 1, grad-accum 8, 3 epochs, lr 2e-4 cosine, warmup 0.03, seed 42 |
| Memory guard | `MAX_FORMATTED_TOKENS=2,000,000` cap on materialized tokens (line 62) |

Loss is computed only on the assistant span (prompt tokens masked to -100).
No CUDA means a clean refusal, never silent CPU training; OOM stops with
checkpoints intact and `start --confirm --resume` reauthorizes the exact
recorded run against its manifest.

### Deployment, immutability, rollback

- `validate_adapter` (`adaptive_training.py:956`) rejects control-character
  paths, symlinks anywhere in the adapter path, base/revision mismatches
  against the reviewed pins, and manifests that do not prove a completed
  authorized run (hashes + sizes re-verified).
- Conversion to GGUF uses llama.cpp pinned to commit
  `99f3dc32296f825fec94f202da1e9fede1e78cf9` (`adaptive_training.py:41`),
  sealed by tree hash and run from an isolated staging snapshot.
- Concurrency: a deployment journal (transition marker bound to the protected
  policy, `adaptive_training.py:1519-1727`) plus an exclusive byte lock
  (`_exclusive_byte_lock`, line 2110, msvcrt/fcntl) serialize the
  endpoint-global `sonder-personal:latest` namespace even across processes
  with different homes.
- **ADR-005 immutability** (`docs/adr/ADR-005-immutable-training-deployment.md`):
  deployments are `sonder-personal:<run-id>`, never a mutation of `:latest`;
  runtime policy owns active model selection via CAS revision;
  **training can never promote itself** — only DeploymentService after
  validation.
- Promotion evidence is `promotion_eval.py` suite `sql-promotion-v2`
  (`promotion_eval.py:22`): held-out executable SQL families plus a dynamic
  instruction probe; pass criteria and QA detail live in
  sonder-validation-and-qa.
- **Rollback** (`_rollback_locked`, `adaptive_training.py:3122`) verifies
  `sonder:latest` exists with a stable digest, then makes a revision-checked
  (CAS) policy-pointer change routing `code`/`general` back to it. It
  deliberately leaves the personal model and all training artifacts in place
  for diagnosis (`TRAINING.md:252-257`). `sonder:latest` itself is never
  overwritten or deleted.

`build_personal_dataset.py` mines a private codebase into local-only JSONL;
its output must never be committed, pushed, or sent to a cloud tier
(`build_personal_dataset.py:9-13`).

## Pitfalls checklist

- Do not "fix" retrieval by lowering `SONDER_MIN_SIM` ad hoc — recalibrate
  with `tune_min_sim.py` and record the derivation, or you recreate the stale
  0.65 incident in the other direction.
- Do not count quarantine loss ROWS; count blame SHARES (one failure blames a
  mean cohort of 3.424 lessons).
- Do not average the caller-judged and machine-graded populations, ever, in
  any new metric or ranking — three separate defects in this repo's history
  came from exactly that blend.
- Do not add a `source` parameter to `record_outcome` or expose
  `record_self_graded_outcome` over MCP.
- Do not report `compiled` as success evidence for training export; it is
  below `GOOD_THRESHOLD` by design.
- Do not run `qlora_train.py` directly, unattended, or from a loop; do not
  fine-tune on already-quantized weights or quantize twice
  (`TRAINING.md:227-236`).
- Do not commit `memory.db`, any `*.jsonl` training export, or anything under
  `sonder-personal-lora/`.

## Provenance and maintenance

Verified against commit 99162cf9 (2026-08-22). All file:line citations were
read from the working tree at that commit; measured corpus figures carry the
dates recorded in the source comments (2026-07-06 and 2026-08-06).

Re-verification one-liners:

- Retrieval thresholds: `python tune_min_sim.py` after large corpus or
  embedding-model changes; compare against `retriever.py:10-25`.
- Quarantine bands: re-run the 2026-08-06 style measurement (scored
  retrievals vs losses per lesson) against the live store before editing
  `QUARANTINE_FREQUENCY_BANDS`; the source comment at `retriever.py:55-71`
  says "Re-measure after large corpus changes."
- Constants drift: `grep -n "DEFAULT_MIN_SIM\|QUARANTINE_\|DUP_THRESHOLD\|ATTRIBUTION_WINDOW" retriever.py reflection.py grounded_outcomes.py`
- Signal prices: `grep -n "SIGNAL_REWARDS" -A 12 sonder_runtime/domain/memory/rules.py`
- Tool line anchors: `grep -n "def record_outcome\|def record_self_graded_outcome" server.py`
- Training pins: `grep -n "HF_REVISIONS\|LLAMA_CPP_REVISION" qlora_train.py adaptive_training.py`
- Lifecycle commands: `python adaptive_training.py plan --dry-run --model auto`
  (stdlib-only; safe on any install) and re-read `TRAINING.md`.
