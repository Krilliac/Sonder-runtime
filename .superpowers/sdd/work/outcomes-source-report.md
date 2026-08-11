# outcomes.source: #62

Branch `work/31-outcomes-source`, base `f93282c`. Four commits, clean checkout,
nothing pushed. No `git stash` (both preserved entries still present), no
`git add -A`, no sibling worktree touched. `scripts/select_regression_tests.py`
is **absent on this lineage** (checked `scripts/`), so files are named
deliberately. Planted repro/mutation files were held in the session scratchpad,
outside the repo.

## Lineage — verified in this worktree

`git merge-base --is-ancestor <x> HEAD`:

| ref | ancestor of HEAD? |
|---|---|
| `feat/verified-fetch-modes-calibration` (`9f377f1`) | **yes** |
| `f93282c` (stated base) | **yes** — HEAD exactly, before my commits |

## The migration machinery — the named defect is NOT fixed on this lineage

`sonder_migrations.migrate_store` still returns early at line 331:

```python
migrations = discover_migrations(store)
if not migrations:
    return status(store, path)
```

`status()` then reports `current=True`, so a release shipped without
`migrations/` journals "ok" while applying nothing. **Confirmed still present
here**; `git log --all -S "if not migrations:"` finds only the commit that
introduced it (`495ca7e`). I did **not** rely on that path being sound: the
column is created by `memory_store._migrate`, which memory.db's schema layer
already owns and which is reached by `connect()`/`init_db()` on every open —
independent of whether `migrations/` shipped. `migrations/memory/0002_outcomes_source.py`
is the ledgered, checksummed record with a `verify()` that fails the migration
if the column is missing, nullable, or carries a DEFAULT. If `migrations/`
vanished from a release the ledger row would be missing, but the column would
still exist and every writer would still be constrained.

## Migration approach — and I did NOT run it against the live store

`ALTER TABLE ... ADD COLUMN` cannot express `NOT NULL` without a `DEFAULT`, and
a default is precisely what must not exist: it is how a writer that forgets
keeps silently filing rows under a meaning it never chose. So
`_migrate_outcomes_source` rebuilds the table (create → copy preserving
`rowid`/`ts` → drop → rename), ordered **before** `_dedupe_outcomes_for_unique_index`
because the rebuild drops the index the dedupe uses as its "already done"
marker. `init_db` recreates all three indexes immediately after, in the same
transaction.

**The operator's store was never migrated, written to, or altered.** It was
opened `mode=ro&immutable=1` for every count. The migration was exercised
against a `sqlite3.Connection.backup()` copy in the scratchpad:

```
LIVE (read-only): 9450 rows, cols [interaction_id, signal, reward, ts]
COPY after migrate (0.03s): cols [..., source]; rows 9450; signal counts identical: True
COPY outcomes by source: {'unknown': 9450}
COPY indexes: idx_outcomes_interaction, idx_outcomes_interaction_signal_reward,
              uq_outcomes_interaction_signal_nonnull
LIVE re-read after all work: cols [interaction_id, signal, reward, ts]; rows 9450
```

## Backfill: everything is `unknown`. I inferred nothing.

**Backfilled: 0 rows. Marked `unknown`: 9,450.** This is the finding, not a
shortcut. What I checked, read-only, before deciding:

| evidence considered | why it does not identify the writer |
|---|---|
| `signal` | `accepted` is written by a caller *and* by `artifact_verify`/`ground_artifact`; `tests_passed` by a caller who ran the tests *and* by the curriculum |
| `interactions.tier` (joined; 29 tier×signal groups) | tiers are model routes, not writers. `tier='example'` (5 rows) is the one writer-specific value — `learn_from_example` — but labelling 5 of 9,450 rows on a join to a prunable table buys nothing and asserts an inference I would have to defend |
| `ts` | no run ledger exists to correlate against |
| interaction id shape | every writer uses `memory_store.new_id()` |
| `lesson_usage` linkage | proves only "the wrapper wrote it", which every caller-facing path does |

The decisive fact: **`curriculum_run.py` and `game_ladder.py` called
`server.record_outcome` — the same caller-facing tool a human uses.** So even
the 9,049 `tests_passed` rows cannot be attributed by shape. A confident label
would be permanently worse than a blank; the whole defect is that nobody can
tell the populations apart.

`lesson_usage.outcome_source` is likewise nullable and unbackfilled: NULL means
both "not yet credited" and "credited before the column", and both read as
unknown provenance.

## Vocabulary — and a correction the tests forced

`caller` / `machine` / `attributed` / `self_curriculum` / `unknown`.

I first modelled this as one `machine` value and excluded it from lesson
eviction. `tests/test_learning_tools.py::test_code_gate_failure_uses_idempotent_atomic_outcome_path`
went RED. **I read it before touching it, and the test was right and my model
was wrong.** The code gate running a reply's own code and finding it broken is
direct evidence the retrieved lesson did not help, and it has driven quarantine
for as long as it has existed; excluding it would have been a gate that
silently stopped firing. The real line is not machine-versus-human but *is the
verdict KNOWN to be about the work the lesson informed*:

* `machine` — the runtime graded **this interaction's own output** (code gate). Eviction-eligible.
* `attributed` — the runtime matched a **later** verification back by project + time window + run id. The link is a heuristic, concurrent same-project runs can cross-match, and this reader has already been found inverted once. Eviction-**ineligible**.

## Consumers filtered, and the thresholds behind them

| consumer | filtered? | threshold that reads it |
|---|---|---|
| `calibration.measure` / `should_verify` / `caution` | **yes** — `POPULATIONS` is now `(signals, sources)` | `MIN_SAMPLE=20`, `POOR_BELOW=0.60`, `GOOD_AT_OR_ABOVE=0.85` → `server._agent_verification_standing`, `finish_final`, the end-report standing line |
| `memory_store.lesson_usage_stats` (epoch walk **and** lifetime `wins`/`losses`/`avg_reward`) | **yes** | `retriever.lesson_quarantine`: `QUARANTINE_MIN_LOSSES`, `QUARANTINE_MIN_DISTINCT_TASKS`, `QUARANTINE_REPEAT_TASK_MIN_LOSSES`, `QUARANTINE_MIN_ATTRIBUTABLE_LOSSES`, `band_loss_rate(wins+losses)` — **live lesson eviction** |
| `memory_store.lesson_usage_history` | **yes** | feeds the above **and** `retriever.attributable_losses` / `usage_stats_with_attribution` |
| `learning_health._outcome_metrics` | **yes** — reviewed/autograded read provenance, not the signal name | `_MIN_REVIEWED_SAMPLE=20`, `_gating_positive_percent`, status gates at `< 60.0` / `< 80.0` |
| `learning_health._reviewed_by_tier` | **yes** — same rule, so the table still sums to `reviewed_outcomes` | the small-sample annotation |
| `server` /quality advisory | **yes** (via the above) | `reviewed >= 30 and reviewed_positive < 60.0`; `reviewed < 30 and outcomes >= 200` |
| `memory_store._distillation_evidence` | **yes, asymmetrically** — see below | whether a lesson is distilled at all |
| `memory_store.outcome_signal_counts` | gained a `sources` parameter | — |
| `memory_store.interaction_outcome_evidence` / `export_training_data` | **no — carries `source`, does not filter** | export policy belongs to the caller; pre-selecting would drop contradictory evidence, the failure its own docstring guards. Surfaced so a policy can decide with the fact in hand |
| `learning_health` `outcome_interactions` / `good_outcome_interactions`; `server` `COUNT(*) FROM outcomes` | **no** | coverage inventories, not rates. A total is a total |
| `refinement_transactions:166`, `memory_store:987` | **no** | existence checks, not rates |
| `recall` contradictory-outcome veto | **no** | dropping a negative to protect a recall is the worse mistake |
| `proposals/metrics_report` | **no** | a proposal; per-signal inventory, gates nothing |

A static AST sweep found **0** production writers omitting `source` and
enumerated all 12 remaining unfiltered reads, each ruled on above.

**A hole I found and closed while surveying:** `_distillation_evidence`'s
`has_good` read *any* good row, so a caller recording a merely weak positive
(`compiled`, 0.70 — not good, not a contradiction) could claim a distillation
whose only good evidence was an `attributed` row nobody reviewed. `has_good`
now excludes `attributed`; `has_contradiction` deliberately does **not**, because
dropping evidence that work was bad to protect a lesson is the worse mistake.
`attributed` may block a lesson, never ground one.

## Writers — omission is now impossible, not discouraged

| writer | source | how omission fails |
|---|---|---|
| `record_outcome` (MCP tool) | `caller` | — |
| `learn_from_example` | `caller` | — |
| `_record_code_gate_failure` | `machine` | — |
| `_record_outcome_signal` (← `attribute`) | `attributed` | — |
| `_drain_deferred_distillations` | the stored row's own source, else `unknown` | re-asserts someone else's verdict; never restamps |
| `curriculum_run` / `game_ladder` | `self_curriculum`, via new in-process `server.record_self_graded_outcome` | — |

Four layers, so a new writer cannot slip through: (1) `outcomes.source` is
`NOT NULL` with a closed `CHECK` and **no DEFAULT** — a raw `INSERT` omitting it
raises `IntegrityError`; (2) `source` is required and **keyword-only** on
`record_outcome_row`, `record_outcome_and_claim_lesson_distillation`,
`record_lesson_usage_outcome`, `_record_outcome_and_maybe_distill`, and the
`MemoryRepository`/`OutcomeStore` ports; (3) `_checked_outcome_source` raises a
naming error rather than a bare IntegrityError; (4) an AST test scans every
production `INSERT INTO outcomes`.

**`record_outcome` gained no `source` argument, on purpose** — provenance a
caller can choose is provenance a caller can misstate. Hence the separate
in-process entry point for the runtime's own drivers.

## `_record_outcome_signal` ruling: routed, for two of the three

It now calls `record_outcome_and_claim_lesson_distillation(..., source='attributed',
claim_distillation=False)`.

* **Taken — the interaction-existence precondition.** No more orphan rows.
* **Taken — the `lesson_usage` credit.** The original objection was that
  `lesson_usage_stats` has no provenance filter and feeds `lesson_quarantine`.
  The credit now carries `outcome_source='attributed'` and the gate excludes
  exactly that source, so the evidence is recorded and visible without reaching
  the gate. That de-blends the metric rather than blending it.
* **Still refused — the distillation claim/cancel.** The reason is *liveness,
  not provenance*: this runs inside `_feed_grounded_outcome` on the
  tool-observation path, which has no way to **finish** a claim. Claiming would
  park the interaction's single distillation slot on a worker that never
  returns; cancelling would let an unreviewed verdict destroy a caller's live
  job.

Measured on the migrated copy, the filter costs the eviction gate nothing
today: `lesson_usage_history` returns **16,191** rows before and after, and
**0** lessons are quarantined either way. The gate binds the moment
`attributed` rows start arriving.

## Mutation results — nine guards planted, observed failing, reverted

| guard | mutation | result |
|---|---|---|
| `outcomes.source` NOT NULL / no DEFAULT | `DEFAULT 'caller'` | **1 failed, 25 passed** |
| required keyword-only `source` | `source='caller'` default | **1 failed, 25 passed** |
| eviction filter, `lesson_usage_history` | predicate made vacuous | **3 failed, 23 passed** |
| eviction filter, `lesson_usage_stats` aggregates | predicate → `1=1` | **1 failed, 25 passed** |
| `calibration` caller population sources | `→ None` (any provenance) | **2 failed, 24 passed** |
| `learning_health` reviewed split | `_REVIEWED_SOURCES` → all sources | **2 failed, 24 passed** |
| `_record_outcome_signal` must not claim | `claim_distillation=True` | **1 failed, 25 passed** |
| `_record_outcome_signal` routes through wrapper | reverted to `record_outcome_row` | **2 failed, 24 passed** |
| `_distillation_evidence` bars `attributed` | ineligible set emptied | **1 failed, 25 passed** |

Each reverted and re-verified; `git status` clean afterwards, no residue.

**Two of these are worth recording as method, not just result.** The
`lesson_usage_stats` aggregate mutation **survived the first round (24 passed)**
— only the epoch walk was covered, while `wins + losses` sets
`lesson_quarantine`'s reference class. A test was added and it now binds. The
`claim_distillation` mutation later survived too, because the new
`_distillation_evidence` filter made it redundant in that setup; the test was
strengthened to seed caller-good evidence first so it decides on its own merits.
Both gaps were invisible to a green suite.

The AST guard is proved against a planted writer held **outside** the repo
tree, so the scanner cannot pass by scanning its own plant.

## Verbatim pytest lines

RED, before any implementation, at 23 items:

```
21 failed, 2 passed in 7.72s     tests/test_outcome_source.py
```

GREEN, final:

```
26 passed in 6.96s               tests/test_outcome_source.py
68 passed in 9.97s               tests/test_codegen_build_loop_server.py tests/test_spec5_memory.py tests/test_legacy_memory_repository.py tests/test_outcome_source.py
159 passed in 9.14s              tests/test_outcome_source.py tests/test_memory_store.py tests/test_learning_tools.py tests/test_reflection.py
922 passed, 1 failed, 3 skipped in 107.53s   (22 named files, the 1 pre-existing — see below)
```

Named regression files (no selector on this lineage): `test_outcome_source
test_memory_store test_learning_health test_calibration test_export test_recall
test_retriever test_learning_tools test_memory_quality test_orchestrator_memory
test_refinement_transactions test_memory_sessions test_server_helpers
test_end_report_standing test_agent_verification_gate test_grounded_outcomes
test_grounded_outcomes_infrastructure test_grounded_outcomes_agent_dispatch
test_reflection test_lesson_decay tests/production/ proposals/metrics_report/`

**The full suite WAS run**, twice, because this changes the storage schema and
a named-file set cannot prove that. Cost **500.62s** on the final run:

```
2 failed, 6412 passed, 46 skipped, 1 warning, 4 subtests passed in 500.62s (0:08:20)
```

Both failures are **pre-existing at `f93282c`**, proven by running them against
a pristine `git archive f93282c` export in the scratchpad:
`tests/production/test_error_signal_ratchet.py` (checker exits 1 at HEAD with
the same two findings) and `tests/test_serve_history.py::test_login_slash_stores_token`
(fails at HEAD on `f1adfac`'s durable-authority refusal).

## Note on the zeros in this report

`calibration.measure(conn, "caller").total == 0` and
`reviewed_outcomes == 0` on the migrated copy are **not** early aborts. The
totals are intact and accounted for: `outcomes == 9450` and
`unknown_source_outcomes == 9450` in the same report, and
`calibration.measure(conn, "execution")` reads `9049 good / 183 bad (98.0%,
n=9232)` on the same connection. The zeros are a reclassification — those rows
have unrecorded provenance and are no longer counted as caller judgements —
which is why `unknown_source_outcomes` ships beside them rather than letting a
0 read as "nobody ever judged anything".

## Commits

```
ff6ef14  Record WHO judged: outcomes.source, backfilled honestly (#62)
1275095  Every writer names its provenance; the bypass is unblocked (#62)
63fe7af  Filter the gates, not just the displays (#62)
4f43bd7  Pin provenance by execution, and prove the guards by mutation (#62)
```

## NEW findings

**Critical — `attributed` evidence could ground a lesson, not just block one.**
Found while surveying reads, not filed anywhere. `_distillation_evidence`'s
`has_good` accepted any good row regardless of provenance, so a caller
recording `compiled` (0.70 — below `GOOD_THRESHOLD`, so neither good evidence
nor a contradiction) could claim a distillation whose only good evidence was a
heuristically-attributed row. A lesson is durable material the retriever serves
for months. Fixed asymmetrically: barred from `has_good`, deliberately kept in
`has_contradiction`.

**Important — the shrink-only `ERROR:` ratchet is already RED on this lineage,
so CI is failing before any of my work.** Measured against a pristine export of
`f93282c`: `scripts/check_error_signals.py` exits 1 with two findings not in the
baseline — `grounded_outcomes.py:197` (`rendered_infrastructure_error`, from
`604e662`) and `server.py` `_agent_dispatch`'s rootless refusal (from
`2cec327`). Both lanes added stringly `ERROR:` sites without updating the
checked-in baseline. Not mine to resolve — fixing it means either editing
another lane's code or relaxing the ratchet — but it must not be discovered as
"the outcomes-source branch broke CI". My own two new `ERROR:` returns were
removed rather than baselined.

**Important — the eviction line is not machine-versus-human, and drawing it
there silently disables a live gate.** The obvious reading of "machine verdicts
must not drive lesson eviction" excludes the code-gate auto-negative, which is
direct evidence about the interaction's own output and has driven quarantine
since it shipped. A test caught it; a green suite would not have. The general
shape: when de-blending a metric, the axis is usually *how firmly the evidence
is linked to the thing being judged*, not *what kind of thing produced it*.

**Important — three more over-narrow doubles, and one hid a floor as a total.**
`tests/test_calibration.py`, `tests/test_agent_verification_gate.py` (×2),
`tests/test_end_report_standing.py` pinned `calibration._counts(_conn)`, and
`tests/test_codegen_build_loop_server.py` pinned
`_record_outcome_and_maybe_distill(iid, signal)` — none asserting anything about
those parameters. This is the same shape as #61's finding, so the pattern is
recurrent rather than incidental. The codegen one is the instructive case:
`_drain_deferred_distillations` swallows per-item exceptions, so the double's
`TypeError` surfaced only as `stored == 0` — the floor-reported-as-total shape
that very test exists to catch, arriving from the test's own double.

**Important — `migrate_store`'s early return is still live on this lineage**
(detailed above), and it now has more to lose: a release missing `migrations/`
would journal "ok" for a store whose ledger never records the provenance
migration. The column itself does not depend on it, but the ledger — the thing
SPEC-2 says is the authority on what a database contains — would be wrong.

**Note, not a finding — a residual the column cannot close retroactively.**
Going forward, `curriculum_run`/`game_ladder` are `self_curriculum`. But every
one of the 9,049 historical `tests_passed` rows they wrote is `unknown` and will
stay so. The reviewed rate is therefore honest but thin on this store until new
rows accumulate, and `unknown_source_outcomes` is what tells an operator that
the emptiness is ignorance rather than a verdict.
