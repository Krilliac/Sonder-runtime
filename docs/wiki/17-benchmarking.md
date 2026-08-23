# Benchmarking the moat

The README makes a claim the rest of the project has to earn:

> The moat isn't the weights — a generically fine-tuned model just re-derives
> what you'd get from Google, and the base model already knows it. The moat is
> **your** private, grounded data.

This page is about turning that sentence into a *measured number*. The harness
lives in [`scripts/benchmark_moat.py`](../../scripts/benchmark_moat.py) and its
offline tests in [`tests/test_benchmark_moat.py`](../../tests/test_benchmark_moat.py).

## What it measures

It runs one small, fixed suite of tasks **three ways against the same model** and
compares the scorecards:

| Arm | What the model sees | Why |
|---|---|---|
| **bare** | the task prompt alone | the raw model with no runtime around it |
| **+runtime (cold)** | the retrieval path, but an **empty** store | the do-no-harm baseline: scaffolding with nothing learned yet |
| **+runtime (warmed)** | the same path, with the lessons/facts a real run would have accumulated | what your data actually buys |

The only thing that differs between arms is the context that reaches the model —
the model, the temperature, the tasks, and the graders are identical. That
isolation is the whole point: any score difference is attributable to
augmentation, not to a different model or a lucky sample.

The report is driven by three deltas:

- **warmed − bare** — the headline "moat" number.
- **cold − bare** — the honesty check. Turning the runtime on before it has
  learned anything must not make the model *worse*. On this harness the cold arm
  reduces exactly to the bare prompt (an empty store injects nothing), so this
  delta is 0 by construction; it exists so a future change that adds always-on
  scaffolding overhead would show up here.
- **warmed − cold** — isolates the warming step from the scaffolding.

## What it does NOT prove

Be honest about the scope, because an over-claimed benchmark is worse than none:

- It measures **retrieval-augmentation lift on a fixed model**, on a **bounded**
  task set with deterministic graders. It is not a general capability benchmark
  and says nothing about a model or task you did not test.
- The default suite's warm lessons are **authored**, not distilled from real
  outcomes. They demonstrate the *mechanism*. On your machine the lift is
  whatever your actual history earns — which is the number that matters, and the
  reason the harness is reproducible against your own warmed store.
- Run with a **fake `model_fn`** (as the tests do), it measures the *harness*,
  not any model. A fake proves the plumbing computes arms, pass-rates, deltas,
  and the scorecard correctly; it proves nothing about intelligence.
- A single run of a stochastic model is a **point estimate**. For a real claim,
  fix the temperature (the CLI defaults to `0.2`), run more than once, and treat
  small deltas with suspicion.

## How to run it

Real run (repo root, runtime venv, Ollama serving the selected model):

```bash
python scripts/benchmark_moat.py                       # Markdown scorecard to stdout
python scripts/benchmark_moat.py --json out.json --markdown card.md
python scripts/benchmark_moat.py --temperature 0.0     # deterministic-as-possible
```

If Ollama is not serving, the CLI exits with a clear message rather than a
cryptic import error — the model surface is built lazily and only when a real run
starts. Importing the module itself never touches a model, opens a DB, or hits
the network, so it is safe to import from tests and other tooling.

As an importable API:

```python
from scripts import benchmark_moat as bm

result = bm.benchmark(bm.DEFAULT_SUITE, my_model_fn)   # model_fn(prompt) -> str
print(bm.render_scorecard(result))
```

You can inject your own tasks, your own `scorer(task, response) -> float`, and
your own `retrieve_fn(task, warm) -> (lessons, facts)` — the last one is how you
point the warm arm at your **real** `memory.db` instead of the authored default
lessons, which is the run that produces a claim about *your* moat.

## How to read the scorecard

The Markdown output has two tables. The first is per-arm:

```
| Arm | Pass rate | Mean score |
| --- | --- | --- |
| bare | 33% (2/6) | 0.42 |
| +runtime (cold) | 33% (2/6) | 0.42 |
| +runtime (warmed) | 83% (5/6) | 0.88 |
```

- **Pass rate** — fraction of tasks whose score met the task's threshold.
- **Mean score** — average grader score in `[0, 1]`. Graders can give partial
  credit (e.g. "mentioned 2 of 3 required facts"), so mean score moves even when
  the pass bit does not — a finer signal than pass-rate alone.

The second table is the deltas:

```
| Delta | Pass rate | Mean score |
| --- | --- | --- |
| warmed - bare (the moat) | +50% | +0.46 |
| cold - bare (do-no-harm) | +0% | +0.00 |
| warmed - cold (warming step) | +50% | +0.46 |
```

A healthy result is: **warmed − bare clearly positive**, **cold − bare at or
near zero**. A negative cold − bare means the scaffolding is costing you
something even empty — investigate before trusting the moat number.

## How the graders work

Every task carries a deterministic grader so a run is reproducible given a fixed
`model_fn`. The built-in factories are substring, "all of" (partial credit),
regex, and integer-parse checks — see
[`scripts/benchmark_moat.py`](../../scripts/benchmark_moat.py). Graders check the
**answer**, never the augmentation text, so a model that already knows the answer
passes every arm and the moat delta is honestly `0` rather than an artifact of
grading the prompt back to itself.

## Comparing grounded-history checkpoints without running a model

`scripts/benchmark_adaptive.py` fills a different evidence gap. It never calls a
model, retrieval system, network, or GPU. Instead, it validates and compares two
bounded records produced by the same external task runner:

- a `fresh` checkpoint with zero grounded-history records and the SHA-256 digest
  of empty history;
- an `accumulated` checkpoint with one or more grounded-history records and the
  digest of that exact checkpoint.

The records are comparable only when the model name and digest, suite name,
version and digest, hardware label and digest, and task-name set all match
exactly. Each task records only `name`, `completed`, `retries`, `tokens_in`, and
`tokens_out`. Summaries and IDs are derived and tamper-checked. Inputs are capped
at 256 tasks and 512 KiB; counters are bounded non-negative integers.

Create `fresh-tasks.json` and `accumulated-tasks.json` as arrays such as:

```json
[
  {"name":"task-a","completed":true,"retries":0,"tokens_in":120,"tokens_out":40}
]
```

Then create and compare the checkpoint records:

```bash
python scripts/benchmark_adaptive.py record \
  --model sonder:latest --model-digest MODEL_SHA256 \
  --suite adaptive-core --suite-version 1 --suite-digest SUITE_SHA256 \
  --hardware host-a/gpu-a/driver-a --hardware-digest HARDWARE_SHA256 \
  --checkpoint fresh --checkpoint-label clean --grounded-records 0 \
  --grounded-history-digest e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855 \
  --tasks fresh-tasks.json --output fresh.json

python scripts/benchmark_adaptive.py record \
  --model sonder:latest --model-digest MODEL_SHA256 \
  --suite adaptive-core --suite-version 1 --suite-digest SUITE_SHA256 \
  --hardware host-a/gpu-a/driver-a --hardware-digest HARDWARE_SHA256 \
  --checkpoint accumulated --checkpoint-label after-100 \
  --grounded-records 100 --grounded-history-digest HISTORY_SHA256 \
  --tasks accumulated-tasks.json --output accumulated.json

python scripts/benchmark_adaptive.py compare --fresh fresh.json \
  --accumulated accumulated.json --json comparison.json \
  --markdown comparison.md
```

The report gives completion, retry, and token deltas plus the exact improved and
regressed task names. A task swap is reported as `mixed` even when aggregate
completion is unchanged, preventing an aggregate score from hiding a regression.
Per-task retry and token regressions are also listed even when the aggregate
direction improves. Retry and token directions remain separate from completion
rather than being collapsed into an unsupported single score. Record IDs bind
the report to both inputs, and the deterministic report has its own content ID.
The CLI refuses output/input path collisions and writes reports atomically.

This path compares supplied observations; it does not create them and does not
claim causation. For credible adaptive-improvement evidence, the external runner
must execute the same task suite with the same model settings and hardware, vary
only the grounded-history checkpoint, and preserve the generated records.

## Checking repository-research evidence without running a model

`scripts/benchmark_repository_research.py` is a complementary, model-free gate
for research-agent output. Its fixed public fixture suite covers known paths and
symbols in the moat benchmark, evaluation history, promotion evaluator, and
model-gateway contract. It requires exact `path` plus `symbol` citations and
reports false claims that no implementation exists, unsupported or invented
evidence, missing fixture coverage, and runs of three or more identical
consecutive tool calls.

The input is bounded to 512 KiB. Candidate answers and tool arguments are never
copied into either report. Unsafe or absolute citation paths are represented by
a redacted marker, while other non-fixture paths and symbols appear only as
SHA-256 identifiers. The JSON and Markdown outputs contain per-case and
aggregate metrics, a digest of the public fixture suite, and a deterministic
report ID. The evaluator does not call a model, inspect private files, or claim
that a passing answer is generally correct beyond the exact fixtures.

The submission contract is:

```json
{
  "schema": "sonder.repository-research-submission.v1",
  "cases": [{
    "id": "benchmark-moat",
    "answer": "The entry points are present.",
    "claims": [{
      "text": "The benchmark function exists.",
      "status": "exists",
      "citations": [{"path": "scripts/benchmark_moat.py", "symbol": "benchmark"}]
    }],
    "tool_calls": [{"tool": "search", "arguments": {"query": "benchmark"}}]
  }]
}
```

Each built-in case and every required citation must be present for a passing
report. Print the fixture contract or evaluate a captured submission with:

```bash
python scripts/benchmark_repository_research.py --print-suite
python scripts/benchmark_repository_research.py --submission result.json \
  --json research-report.json --markdown research-report.md
```

Keep this report separate from adaptive benchmark identity. A controlled runner
may use the research case pass/fail result when it creates the same named task in
fresh and accumulated observations, but it must still supply retries and token
counts and satisfy every model, suite, hardware, task-set, and grounded-history
identity check in `benchmark_adaptive.py`. This harness neither mutates those
records nor changes their comparability rules.

## Related

- [Memory & Learning](06-memory-and-learning.md) — how lessons and facts are
  distilled from grounded outcomes and retrieved; the warm arm exercises exactly
  this path.
- [Model Tiers & Gateway](08-model-tiers-and-gateway.md) — the model the harness
  drives every arm through.
- [Training](15-training.md) — the other axis of improvement (adapters); this
  harness measures retrieval lift, not adapter lift.
- Root [README](../../README.md) — the "moat" claim this page exists to measure.

## Reproducible provider/model matrices

The application-level harness in
`sonder_runtime.application.evaluation.reproducible` fills the gap between the
specialized benchmark scripts above and the existing proposal lifecycle. It
provides:

- immutable scenario IDs and versions, digest-bound golden cases, and an
  immutable-by-version registry;
- explicit provider/model/revision identities and deterministic target
  ordering for matrix runs;
- per-case pass, assertion, timeout, provider, protocol, and invalid-response
  outcomes;
- replayable `TrajectoryRecord` evidence using the existing evaluation replay
  contract;
- absolute pass/error/timeout thresholds plus pass-rate-drop and per-case
  regression gates against an exact-scenario baseline; and
- a bridge from a run report to the existing `EvaluationResult` proposal
  lifecycle contract.

The checked-in public fixtures under `tests/fixtures/evaluation/` prove the
complete offline path. Run them without Ollama, a GPU, or network access:

```bash
python scripts/run_reproducible_eval.py \
  --scenario tests/fixtures/evaluation/scenario.local-tools.v1.json \
  --provider tests/fixtures/evaluation/provider.local-reference.v1.json \
  --output .local/evaluation-matrix.json
```

Repeat `--provider` to build a provider/model matrix. Add `--baseline` with a
previous single-run report or single-target matrix to enable relative
regression gates. The command
returns `0` when every target clears its gates, `1` for a measured regression,
and `2` for invalid/tampered fixtures or harness errors.

Provider fixtures are deterministic request/result tables. They validate the
harness and CI plumbing; they do not claim model quality. Real model adapters
must implement the same injected provider contract, enforce their request
deadline, and pin a model/provider digest.

Run reports intentionally omit raw values from the diagnostic summary. The
saved matrix includes raw trajectory inputs and outputs because those are
required for replay. Treat it like prompt/response data: choose the destination
explicitly, review it before sharing, and never commit private evaluations.
