# Speculation & Prediction

CPU-microarchitecture ideas applied to the agent loop's serial latency
(the model decides → a tool runs → the model decides again). All of it is
**advisory**: correctness never depends on a prediction being right.
`sonder_speculation.py`. Disable with `SONDER_SPECULATION=0`.

## Branch prediction

A history-indexed predictor learns, from grounded runs, which tool tends
to follow a given loop state and which tier a request kind opens with:

- **Next-tool table** keyed by a compact loop state (phase, last tool,
  tool-set seen) — the analog of a CPU's global-history predictor.
- **Opener table** keyed by request kind (debug/author/search/explain).
- Predictions below a confidence floor are suppressed. The table persists
  as a small bounded JSON under `SONDER_HOME` so it warms across runs.

Measured on repetitive agent workloads: 100% next-tool accuracy after one
warm-up run.

## Speculative execution

While the model is generating its next decision, the host may
speculatively run the predicted call — **only for read-only tools** on a
strict allowlist, exactly as a CPU speculates loads but never retires
stores past an unresolved branch:

- If the model commits to the **same** call signature, the buffered
  observation is **retired** — the tool ran once, speculatively, its
  latency hidden inside model generation.
- Otherwise it is **squashed** and discarded.
- Mutations, execution, web, and model-spawning tools are unspeculatable
  by omission. A mispredict costs at most one wasted read-only call and
  never touches durable state.

## File prefetcher (argument-level)

A stream-prefetcher one level up from the CPU analogy: agents list files,
then read them roughly in listing order. The prefetcher parses observed
listings (`file_find`, `directory_tree`, inventory, `script_search`),
waits for one read to confirm the stream, then predicts the next unread
entry as a concrete `file_read` — unlocking speculation for the tools
where read-only time actually lives. Integration-tested: in a
list-then-read run, prefetched reads retire with each file dispatched
exactly once.

## Model prewarm

`server.prewarm_model()` fires the local model load in a daemon thread as
soon as the serve path resolves a tier, overlapping cold-load latency
(tens of seconds for a 7B on CPU) with the host's history/recall/
augmentation work — a prefetch of the weights the pipeline will need.
Local tiers only, single-flight per model, best-effort.

## Adaptive cost model (self-tuning by hardware)

The wall time a correct speculation hides is the **shorter** of the two
latencies — you can hide at most the whole tool inside the decision, or the
whole decision behind the tool:

```
hidden ≈ min(model_decision_time, tool_time) × prediction_confidence
```

The predictor measures both latencies *on the machine it actually runs on*
(exponentially-weighted, persisted across runs) and only issues a
speculation when the expected hidden time clears a floor
(`SONDER_SPECULATION_MIN_SAVING_MS`, default 40 ms). A short warmup lets it
take real samples before it starts gating. The result is one behavior that
tunes itself to the deployment, with no per-machine setting:

| Regime | Decision | Tool | `min(·)` | Behavior |
|---|---|---|---|---|
| Laptop CPU + small model | seconds | ~ms | ~ms | **dormant** — nothing worth hiding |
| GPU + 7B | ~1 s | ~ms | ~ms | mostly dormant |
| **Serious HW + large local model** (multi-B/T) + real tools | seconds | seconds | **seconds** | **engaged** — meaningful wall time hidden every step |
| Hosted/cloud reasoning tier + big scans | seconds | seconds | seconds | engaged (though cloud tiers disable *tool* speculation for budget safety) |

This is the honest answer to "does speculation help?": on a laptop, no, and
it now correctly declines to waste work there; on serious hardware running a
genuinely capable local model with genuinely slow tools — the regime Sonder
is explicitly built to scale up into — it hides real seconds per step. The
CPU analogy holds exactly: speculation never made a slow unit fast, it keeps
fast units busy, and the win grows with the gap between a fast decision and
a slow memory access.

## Model prewarm on big models

Prewarm matters *more* as models get bigger: a large local model can take
tens of seconds to minutes to page into VRAM. `prewarm_model()` overlaps
that cold-load with the host's history/recall/augmentation work so the first
real token isn't waiting on the weights. On a keep-resident deployment
(`OLLAMA_KEEP_ALIVE` set high) the heavy tier stays warm between requests.

## What it exposes

`/status` reports predictor accuracy, speculation issue/retire rates,
learned-state counts, the **measured** decision/tool latencies, and the
**cumulative wall time actually hidden** — so a deployment can see, with
numbers rather than claims, whether it has crossed into the paying regime.
