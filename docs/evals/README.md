# eval_harness — offline-first scenario evaluation

`eval_harness.py` is the shared machinery under Sonder's `eval_*` script
family: a scenario registry, deterministic replay fixtures, a provider
matrix, structured per-case outcomes, replayable traces, a checked-in
regression baseline, a run comparison, and trials. The default run is fully
offline; anything that touches the local model server requires an explicit
`--live`. Two suites ship and both run as gates in CI (`.github/workflows/ci.yml`,
job `tests`): `smoke_python` replays a checked-in cassette through the real
repair loop, and `tool_policy_gates` sends recorded tool proposals through
the runtime's real permission gate.

## Quickstart

```bash
# list registered suites (eval_scenarios/*.json) with their scenario kinds
python eval_harness.py list

# run the shipped smoke suite against its replay cassette, gate on the ratchet
python eval_harness.py run --suite smoke_python --check-baseline

# run the shipped policy suite: no model, the runtime's own permission gate
python eval_harness.py run --suite tool_policy_gates --check-baseline

# prove two runs of a suite are step-identical (trajectory comparison)
python eval_harness.py verify-replay --suite smoke_python

# classify every case between two run directories (before, then after)
python eval_harness.py compare --run eval_runs/smoke_python-1 --run eval_runs/smoke_python-2

# repeat every case and report pass@1 beside pass@k (meant for --live runs)
python eval_harness.py run --suite smoke_python --provider ollama:qwen2.5-coder:7b --live --trials 3

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
python eval_harness.py run --suite tool_policy_gates --out /tmp/p
python eval_harness.py baseline update --run /tmp/p --provider policy
```

Exit codes follow the repo convention: `0` clean, `1` baseline violation (or
`--strict` with any non-pass, or `compare` with a regression), `2`
harness/infrastructure error (invalid suite, unrecordable history that was
explicitly requested).

## What a run produces

```
eval_runs/<suite>-<ts>/
  report.md            # human failure report (matrix + per-failure sections)
  failures.json        # machine-readable failure list
  <provider>/
    summary.json       # sonder.eval-harness.run/v1 (suite_hash, digests, totals, trials)
    results.jsonl      # one structured case outcome per line
    traces/<id>.jsonl  # full replayable trace: prompts, responses, exec results
    comparison.json    # written here by `compare` when this run is the "after"
```

Every case ends in exactly one of seven **never-merged outcome classes**
(the discipline from `scripts/benchmark_schema_offload.py`; the vocabulary
lives in `sonder_runtime/application/evaluation/harness_outcomes.py`):

| status | class | meaning |
|---|---|---|
| `pass` | graded | the solution passed its check under real execution, or the gate decided as expected |
| `fail` | graded | attempts ran and the check failed (`assertion`, `execution`, `exec_timeout`, `no_code`), or the gate decided otherwise (`policy_mismatch`) |
| `error` | infrastructure | `cassette_miss` or `provider_error` — never a graded zero |
| `timeout` | infrastructure | the case exceeded its wall-clock ceiling before any attempt finished |
| `verifier_unavailable` | infrastructure | the named verifier could not judge (its tool is absent) |
| `unknown` | infrastructure | the harness itself crashed between "started" and "graded" (`harness_crash`) |
| `abandoned` | infrastructure | a live (non-deterministic) provider hit the wall clock after at least one attempt |

`pass_rate` is computed over **graded** cases only; the infrastructure
classes are counted individually, summed under `infra`, and gated by the
baseline's `forbid_infra`. They are never averaged away.

## Scenario registry

Suites are JSON files in `eval_scenarios/` with schema
`sonder.eval-harness.suite/v1`. Two scenario kinds exist; unknown kinds are
rejected at load so a half-supported scenario cannot silently no-op.

**`python_function`** — `{id, prompt, check, timeout_s, max_attempts, tags,
verifier?, verifier_spec?}`. `check` is an assert block executed against the
extracted solution by `grounding.run_code` (fresh subprocess, scratch cwd,
clamped timeout — failure isolation, not a security sandbox). The repair
loop is `solver.solve`. A scenario may instead name a `verifier` from
`verifiers.REGISTRY` (`python_exec`, `program_run`, `pytest_run`,
`typecheck`, `cpp_compile`, `llm_judge`); the loop is then
`solver.solve_verified`, `verifier_spec` is passed to the backend with the
scenario's `check` filled in, and a backend that cannot judge yields
`verifier_unavailable` rather than a verdict. `builtin_tasks` pulls entries
from `training_tasks.TASKS` by name instead of copying them.

**`tool_policy`** — `{id, tool, mode, surface, interactive?, expected,
arguments?, rules?, tags}`: a recorded tool proposal and the decision it
must produce. `mode` is one of the four permission modes, `surface` one of
`agent`, `loop`, `control`, `mcp`, `native-mcp`, `http`, `repl`, and
`expected` one of `allow`, `refuse`, `ask` (`ask` only with
`interactive: true`, which only `repl` and `control` may claim). `rules` is
an explicit `[{pattern, action}]` list; absent, the shipped defaults apply.
The runner asks the real gate — the agent gate itself for `agent`,
`decide_for_caller` with that surface's gate-control exemption for the
others, plain `decide` for the loop — under the scenario's mode and rules,
never the operator's, and records nothing: an evaluation must neither flip
the mode a person is running under nor leave receipts that read as real
unattended activity. No model call, no cassette. The shipped
`tool_policy_gates` suite pins the unattended-authority contract, including
the case that turned red when an unattended `ask` stopped degrading to
allow (`file_write_unattended_is_refused_in_manual`).

`suite_hash` is a sha256 over everything that defines what the suite
measures (ids, prompts, checks, timeouts, attempts, verifiers, and every
policy field — not tags or prose), in the style of
`promotion_eval.SUITE_HASH`. A `python_function` scenario without a verifier
hashes exactly as it always did, so existing baselines keep holding. The
baseline pins the hash, so editing a suite without re-baselining fails
loudly as `suite_changed`.

## Providers and determinism

- `replay` (default): serves recorded responses from
  `eval_scenarios/cassettes/<suite>.cassette.json`. Entries are keyed by
  **(scenario, call index)**, not prompt hash — repair prompts embed real
  tracebacks whose tempfile paths differ per run, so hash keying would miss
  on every replay. Recorded prompt digests are kept as *advisory* metadata:
  a mismatch is counted as `cassette_drift` (the suite or solver templates
  changed since recording) without failing the case. A missing recording is
  a loud `cassette_miss` error.
- `policy`: the runtime's own permission policy, used automatically for a
  suite whose scenarios are all `tool_policy` (so `replay` on such a suite
  means the same thing). Its content digest is the sha256 of the policy
  sources (`permission_modes.py`, `permission_rules.py`, the domain rule
  evaluator and the command catalog), so history recorded for a policy suite
  is pinned to the policy that produced it. Naming it for a suite that needs
  a model is refused.
- `ollama:<model>`: live local generation through `server._make_generate`
  (temperature 0). Requires `--live`. Its provenance digest is the exact
  Ollama manifest digest via `promotion_eval.local_model_digest`.
- `CallableProvider` (API only): adapts any `prompt -> text` callable for
  tests and custom baselines.

The repair loop is real in every mode: `solver.solve` executes the model's
code, feeds the true failure output back, and retries — the shipped
`smoke_python` cassette deliberately contains a buggy first `slugify`
response so the repair path is exercised offline on every CI run.

## Trials

`--trials k` (1..10) runs every case `k` times. The case's own status is
the **first** trial's, so `required_pass` in the baseline stays a statement
about one honest run; every trial's status is kept on the case, and
`pass_at_k` (any trial passed) is reported beside `pass` in the totals and
the report. On the replay and policy providers the trials are identical by
construction and the report says so; the flag exists for `--live` runs,
where a model's variance is the thing being measured. The trace written for
a case is its first trial's.

## Replayable traces

Each case writes a JSONL trace (`sonder.eval-harness.trace/v1`): the exact
prompts, responses, execution or verifier results — or, for a policy case,
the decision with its risk and source — closed by a
`sonder.evaluation-trajectory.v1` record (digests and booleans only, so
tempfile noise cannot fake divergence). `verify-replay` runs a suite twice
and compares trajectories with the existing
`sonder_runtime.application.evaluation.trajectory_replay` comparator —
divergence means nondeterminism crept into scenarios, graders, or runner.
Traces contain raw prompts and responses; run output stays out of git
(`eval_runs/` is ignored) and on the local machine.

## Comparing two runs

`compare --run A --run B` joins the two runs' `summary.json` by scenario id
and classifies every case `same`, `regressed`, `improved` or `infra` (either
side an infrastructure outcome). Where both runs wrote a trace, the
trajectory comparator decides step-level divergence; otherwise the
trajectory digests do. The result (`sonder.eval-harness.comparison/v1`,
written to the second run as `comparison.json` or to `--out`) names every
regressed id and carries reason codes: `case_regressions`,
`suite_mismatch` and `case_set_mismatch` fail the comparison (exit 1);
`pass_rate_drop`, `infrastructure_outcomes` and `trajectory_divergence` are
reported but do not, because two honest runs of a live provider differ in
those ways without either being wrong. The embedded `assessment` reuses the
`RegressionAssessment` record of
`sonder_runtime/application/evaluation/reproducible.py`, so the two
evaluation lanes share one comparison shape.

## Regression baseline

`eval_scenarios/eval_baseline.json` (`sonder.eval-harness.baseline/v1`) is a
checked-in ratchet in the style of `scripts/error_signal_baseline.json`: per
suite × provider it pins `suite_hash`, a `min_pass_rate` floor over graded
cases, a `required_pass` scenario list, and `forbid_infra` (which covers all
five infrastructure classes). A suite/provider pair *absent* from the
baseline is itself a violation — an unbaselined suite silently passing is
the exact failure mode the ratchet exists to prevent. Updates are always
explicit (`baseline update`).

## Durable history

A run's aggregate (graded cases only) is appended to the durable evaluation
history via `sonder_runtime.adapters.evaluation_history_store` — locked,
atomic, digest-verified — **by default wherever that is honest**: the
provider carries a real 64-hex content digest (a cassette digest, the policy
source digest, or an Ollama manifest digest) and the run graded something.
Otherwise the run says why it skipped. `--record-history` insists (and exits
2 when the digest is not honest, as before); `--no-record-history` never
touches the history, which is what CI passes. The identity's suite is
namespaced `eval-harness:<suite>` so harness records can never blend into
promotion-eval history groups.

## The CI gate

The `tests` job runs both shipped suites offline with `--check-baseline
--no-record-history` before the pytest suite and uploads their run
directories (`eval_runs/ci/`) as the `eval-runs` artifact whether or not
they pass. A baseline violation fails the job with exit 1 and a harness
error with exit 2, so a regression in the fixtures, the graders, the repair
loop or the permission gate is a red build with a report attached, not a
number in a log.

## What this does NOT prove

- A green replay run proves the **harness, scenarios, graders, and
  fixtures** are healthy — not that any model is good.
- A green policy run proves the permission gate decides as the suite says
  under the scenario's mode and rules — not that every surface consults it.
- Promotion decisions stay with `promotion_eval.promotion_decision`; this
  harness never promotes, demotes, or reconfigures anything.
- Live-provider results are as repeatable as the backend allows
  (temperature 0), but are still declared non-deterministic.

## Provenance in every artifact

Every artifact carries a `schema` string and a content digest
(`suite_hash`, cassette or policy digest, `report_id`, `trajectory_digest`),
plus the git revision of the run — the same convention as
`sonder.promotion-evaluation/v1` and `sonder.eval-history.v1`.

## Design lineage (adapted, not imported)

The mechanisms borrow deliberately from established harnesses, grounded in
this repo's existing contracts, with zero new dependencies: per-sample
structured outcomes and trace logs (Inspect AI), execution-grounded grading
against hidden checks (SWE-bench/tau-bench), cassette record/replay and
checked-in threshold gating (Promptfoo, Braintrust, OpenAI Evals),
pass@k over repeated trials (HumanEval), and graded-vs-infrastructure
outcome separation (DeepEval/Ragas practice, and this repo's own
`benchmark_schema_offload`). The `kind` field and the provider protocol are
the extension seams; both fail closed on anything they do not recognize.

Status: the harness, its outcome vocabulary and both shipped suites are
covered by `tests/test_eval_harness.py`, `tests/test_eval_harness_outcomes.py`,
`tests/test_eval_harness_policy.py` and `tests/test_eval_harness_e2e.py`
(offline, run in CI). The live `ollama:` provider path and the `record`
command are **experimental**: they reuse proven building blocks
(`server._make_generate`, `promotion_eval.local_model_digest`) but have no
automated test coverage, since tests never touch a model.
