# Prefix-cache telemetry and prefix prewarm evidence — 2026-09-29

## Requirement

Roadmap Phase 1: provider prompt-cache reuse on the main chat path was
unmeasured (#510 ledger F5). The logical prefix-manifest decision
(`PrefixManifestCache`, see [CTX-009 prefix caching](CTX-009-PREFIX-CACHE-2026-09-23.md))
and the provider-reported `prompt_eval_cached_count` existed separately and
were never joined, and `prewarm_model` loaded weights without prefilling the
stable prefix.

## What changed

- `sonder_runtime/application/prefix_cache_report.py` joins, per
  (provider, model), the logical decision (`hit`, `cold_start`,
  `identity_changed`, `version_changed`, `prefix_changed`) and the sections
  that changed with the provider's cached/prompt token counts, e.g.
  `prefix_changed[emotions], 2194/2657 cached` or `hit, 2510/2528 cached`.
  Counts are never manufactured: missing, bridged, or inconsistent
  provider counts are reported as `unmeasured`.
- `sonder_runtime/adapters/inference/prefix_cache.py` records that join for
  every `_answer` turn (fail-soft) and publishes two additive Prometheus
  series with closed label sets:
  - `sonder_prefix_cache_total{provider,reason,reuse}` — counter;
    `provider` in `ollama|ollama_cloud|bridged|other`, `reason` one of the
    manifest reasons or `other`, `reuse` in `none|partial|unmeasured|other`.
  - `sonder_prefix_cached_ratio{provider,reason}` — histogram of
    cached/prompt tokens, observed only when the provider measured both.
- Local system-prompt section order is now identity, profile, request system,
  emotion vectors, active goal (was: identity, profile, emotion vectors,
  active goal, request system). Every section's text is unchanged; only the
  position of the request-scoped section moves ahead of the two volatile
  sections. The cloud path (identity + request system only) is unchanged.
- `prewarm_model` (still behind the existing prewarm gate) sends a
  `num_predict: 1` `/api/chat` with the local system prompt and the same
  runner options the turn uses (`num_ctx`, `num_gpu`, ...), so the stable
  prefix is resident in the provider KV cache. If the prefix cannot be built,
  it falls back to the historical weight-only load. The main chat payload
  carries no tool schemas, so the prefill carries none either.

## Measurements

Local Ollama 0.34.4, `hf.co/katanemo/Arch-Router-1.5B.gguf:Q4_K_M`,
`num_gpu: 0`, `num_ctx: 8192`, model unloaded (`keep_alive: 0`) before each
scenario. Two consecutive turns with the real identity block and
`system_profile.md`; turn two appends the assistant reply and a new user turn.
Node1's Ollama (0.33.2) does not report `prompt_eval_cached_count`, so these
runs were local.

| second-turn change | request system | old order cached/prompt | new order cached/prompt |
|---|---|---|---|
| none | empty | 2634/2657 (170 ms eval) | 2634/2657 |
| emotion vectors | empty | 2194/2657 (2491 ms) | 2194/2657 |
| goal note added | empty | 2529/2665 (838 ms) | 2529/2665 |
| none | persona + trace | 2771/2794 (188 ms) | 2771/2794 |
| emotion vectors | persona + trace | 2194/2794 (3320 ms) | 2331/2794 (2624 ms) |
| goal note added | persona + trace | 2529/2802 (1583 ms) | 2666/2802 (817 ms) |

A volatile section invalidates the provider cache for every token after it;
moving the request system ahead of it recovers exactly its ~137 tokens.

First turn after prewarm (same model, runtime `_make_generate` path,
`num_ctx` 32768 as chosen by `_auto_model_context`), two runs each:

| prewarm | first-turn cached/prompt | prompt eval | load |
|---|---|---|---|
| weight-only (old) | 0/2528, 0/2528 | 6201 ms, 6169 ms | 1807 ms, 2331 ms |
| prefix prefill (new) | 2510/2528, 2510/2528 | 96 ms, 108 ms | 15 ms, 33 ms |

The prefill itself costs one prompt evaluation (about 8 s on CPU for this
model) off the request path.

## Verification

- `_build_system` over a 384-case matrix (profile/emotion/goal presence,
  cloud, trace, persona, request system, model, provider), origin/main versus
  this change: cloud 192/192 byte-identical; local 80 identical and 112
  reordered with an identical multiset of lines and of `\n\n` blocks; 0 cases
  with any line added, removed, or edited.
- Focused tests: `tests/test_prefix_cache_report.py`.

## Limitations

- Evidence is from a 1.5B CPU model; absolute timings differ on the GPU
  models, cached-token behaviour follows the same prefix rule.
- The emotion-vector and goal sections still bust everything after them
  when they change; they are last so that is only themselves plus the
  conversation.
- The join is in-process and per (provider, model); it resets on restart.
