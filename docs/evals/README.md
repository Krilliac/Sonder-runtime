# eval_harness — offline-first scenario evaluation

`eval_harness.py` is the shared machinery under Sonder's `eval_*` script
family: a scenario registry, deterministic replay fixtures, a provider
matrix, structured per-case outcomes, replayable traces, and a checked-in
regression baseline. The default run is fully offline; anything that touches
the local model server requires an explicit `--live`.

## Quickstart

```bash
# list registered suites (eval_scenarios/*.json)
python eval_harness.py list

# run the shipped smoke suite against its replay cassette, gate on the ratchet
python eval_harness.py run --suite smoke_python --check-baseline

# prove two replay runs are step-identical (trajectory comparison)
python eval_harness.py verify-replay --suite smoke_python

# run the same suite against a live local model as well (provider matrix)
python eval_harness.py run --suite smoke_python \
    --provider replay --provider ollama:qwen2.5-coder:7b --live

# focus or chunk-resume (eval_retrieval style) — the narrowed run recomputes
# its suite_hash, so a partial run can never satisfy a full-suite baseline
# pin or blend into the full suite's history identity
python eval_harness.py run --suite smoke_python --only slugify
python eval_harness.py run --suite smoke_python --start 2 --count 2

# record a fresh cassette from a live model (explicit, never automatic)
python eval_harness.py record --suite smoke_python \
    --model qwen2.5-coder:7b --live

# after deliberately changing a suite or its expected outcomes:
python eval_harness.py run --suite smoke_python --out /tmp/r
python eval_harness.py baseline update --run /tmp/r
```

Exit codes follow the repo convention: `0` clean, `1` baseline violation (or
`--strict` with any non-pass), `2` harness/infrastructure error (invalid
suite, unrecordable history).

## What a run produces

```
eval_runs/<suite>-<ts>/
  report.md            # human failure report (matrix + per-failure sections)
  failures.json        # machine-readable failure list
  <provider>/
    summary.json       # sonder.eval-harness.run/v1 (suite_hash, digests, totals)
    results.jsonl      # one structured case outcome per line
    traces/<id>.jsonl  # full replayable trace: prompts, responses, exec results
```

Every case ends in one of four **never-merged outcome classes** (the
discipline from `scripts/benchmark_schema_offload.py`):

| status | meaning |
|---|---|
| `pass` | graded: the solution passed its check under real execution |
| `fail` | graded: attempts ran and the check failed (`assertion`, `execution`, `exec_timeout`, `no_code`) |
| `error` | infrastructure: `cassette_miss` or `provider_error` — never a graded zero |
| `timeout` | infrastructure: the case exceeded its wall-clock ceiling |

`pass_rate` is computed over **graded** cases only; `error`/`timeout` counts
are reported separately and gated by the baseline's `forbid_infra`.

## Scenario registry

Suites are JSON files in `eval_scenarios/` with schema
`sonder.eval-harness.suite/v1`. A scenario is `{id, kind, prompt, check,
timeout_s, max_attempts, tags}`; `check` is an assert block executed against
the extracted solution by `grounding.run_code` (fresh subprocess, scratch
cwd, clamped timeout — failure isolation, not a security sandbox).
`builtin_tasks` pulls entries from `training_tasks.TASKS` by name instead of
copying them. The only `kind` today is `python_function`; unknown kinds are
rejected at load so a half-supported scenario cannot silently no-op.

`suite_hash` is a sha256 over everything that defines what the suite
measures (ids, prompts, checks, timeouts, attempts — not tags or prose), in
the style of `promotion_eval.SUITE_HASH`. The baseline pins it, so editing a
suite without re-baselining fails loudly as `suite_changed`.

## Providers and determinism

- `replay` (default): serves recorded responses from
  `eval_scenarios/cassettes/<suite>.cassette.json`. Entries are keyed by
  **(scenario, call index)**, not prompt hash — repair prompts embed real
  tracebacks whose tempfile paths differ per run, so hash keying would miss
  on every replay. Recorded prompt digests are kept as *advisory* metadata:
  a mismatch is counted as `cassette_drift` (the suite or solver templates
  changed since recording) without failing the case. A missing recording is
  a loud `cassette_miss` error.
- `ollama:<model>`: live local generation through `server._make_generate`
  (temperature 0). Requires `--live`. Its provenance digest is the exact
  Ollama manifest digest via `promotion_eval.local_model_digest`.
- `CallableProvider` (API only): adapts any `prompt -> text` callable for
  tests and custom baselines.

The repair loop is real in every mode: `solver.solve` executes the model's
code, feeds the true failure output back, and retries — the shipped
`smoke_python` cassette deliberately contains a buggy first `slugify`
response so the repair path is exercised offline on every CI run.

## Replayable traces

Each case writes a JSONL trace (`sonder.eval-harness.trace/v1`): the exact
prompts, responses, and execution results, closed by a
`sonder.evaluation-trajectory.v1` record (digests and booleans only, so
tempfile noise cannot fake divergence). `verify-replay` runs a suite twice
and compares trajectories with the existing
`sonder_runtime.application.evaluation.trajectory_replay` comparator —
divergence means nondeterminism crept into scenarios, graders, or runner.
Traces contain raw prompts and responses; run output stays out of git
(`eval_runs/` is ignored) and on the local machine.

## Regression baseline

`eval_scenarios/eval_baseline.json` (`sonder.eval-harness.baseline/v1`) is a
checked-in ratchet in the style of `scripts/error_signal_baseline.json`: per
suite × provider it pins `suite_hash`, a `min_pass_rate` floor over graded
cases, a `required_pass` scenario list, and `forbid_infra`. A suite/provider
pair *absent* from the baseline is itself a violation — an unbaselined suite
silently passing is the exact failure mode the ratchet exists to prevent.
Updates are always explicit (`baseline update`), mirroring the
`--record-history` posture of `eval_models.py`.

## Durable history

`run --record-history` appends the aggregate (graded cases only) to the
durable evaluation history via
`sonder_runtime.adapters.evaluation_history_store` — locked, atomic,
digest-verified. The identity's suite is namespaced `eval-harness:<suite>`
so harness records can never blend into promotion-eval history groups, and
recording is refused unless the provider has a real 64-hex content digest
(a replay cassette digest or an Ollama manifest digest).

## What this does NOT prove

- A green replay run proves the **harness, scenarios, graders, and
  fixtures** are healthy — not that any model is good.
- Promotion decisions stay with `promotion_eval.promotion_decision`; this
  harness never promotes, demotes, or reconfigures anything.
- Live-provider results are as repeatable as the backend allows
  (temperature 0), but are still declared non-deterministic.

## Provenance in every artifact

Every artifact carries a `schema` string and a content digest
(`suite_hash`, cassette digest, `report_id`, `trajectory_digest`), plus the
git revision of the run — the same convention as
`sonder.promotion-evaluation/v1` and `sonder.eval-history.v1`.

## Design lineage (adapted, not imported)

The mechanisms borrow deliberately from established harnesses, grounded in
this repo's existing contracts, with zero new dependencies: per-sample
structured outcomes and trace logs (Inspect AI), execution-grounded grading
against hidden checks (SWE-bench/tau-bench), cassette record/replay and
checked-in threshold gating (Promptfoo, Braintrust, OpenAI Evals), and
graded-vs-infrastructure outcome separation (DeepEval/Ragas practice, and
this repo's own `benchmark_schema_offload`). The `kind` field and the
provider protocol are the extension seams; both fail closed on anything
they do not recognize.

Status: the harness and its smoke suite are covered by
`tests/test_eval_harness.py` and `tests/test_eval_harness_e2e.py` (offline,
run in CI). The live `ollama:` provider path and `record` command are
**experimental**: they reuse proven building blocks
(`server._make_generate`, `promotion_eval.local_model_digest`) but have no
automated test coverage, since tests never touch a model.
