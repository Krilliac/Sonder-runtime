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

## Related

- [Memory & Learning](06-memory-and-learning.md) — how lessons and facts are
  distilled from grounded outcomes and retrieved; the warm arm exercises exactly
  this path.
- [Model Tiers & Gateway](08-model-tiers-and-gateway.md) — the model the harness
  drives every arm through.
- [Training](15-training.md) — the other axis of improvement (adapters); this
  harness measures retrieval lift, not adapter lift.
- Root [README](../../README.md) — the "moat" claim this page exists to measure.
