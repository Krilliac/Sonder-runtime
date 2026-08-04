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

## When it pays (honest)

Savings per step ≈ the time of the tool being overlapped. On the CPU
sandbox, read-only tools cost milliseconds against multi-second decisions,
so the measured end-to-end gain today is ~0%. The value scales with:

1. **slower tools** — large monorepos, network filesystems (searches in
   seconds, not milliseconds);
2. **faster models** — on GPU, decisions shrink so tool time is a larger
   fraction to hide;
3. **more predictable decisions** — a capable model raises predictor
   accuracy and, if hosted, adds decision latency to hide behind.

`/status` exposes predictor accuracy, speculation issue/retire rates, and
learned-state counts, so a deployment can see when it crosses into the
paying regime. The honest CPU analogy: speculation never made a slow unit
fast — it keeps fast units busy.
