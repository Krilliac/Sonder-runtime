# Sonder Inference request path: streaming, thinking, sampling, residency

How a chat turn served by the `sonder_inference` provider reaches the user as
fast as the local GPU allows, and which switches control it. Provider
reference: [sonder-inference-provider](../architecture/sonder-inference-provider.md).
Verified against the code on 2026-09-30.

| Concern | Module |
|---|---|
| Live turn stream (claim, hold-back, reconcile) | `sonder_runtime/application/chat/stream_sink.py` |
| Streaming transport (SSE parse, aggregate, cancel) | `sonder_runtime/adapters/inference/sse_stream.py` |
| Early-SSE delta writer and terminal frames | `sonder_runtime/interfaces/http/live_stream.py` |
| Thinking forwarding, sampling defaults | `sonder_runtime/adapters/inference/request_tuning.py` |
| Primary-model residency, GPU contention | `sonder_runtime/adapters/inference/gpu_residency.py`, doctor check `sonder_inference_gpu` |

## Live token streaming (on for `sonder_inference`)

A streamed HTTP chat turn (`POST /v1/chat/completions`, `"stream": true`)
commits to SSE before generating. When the turn's rung is bridged to
`sonder_inference`, the first bridged generation of the turn now asks Sonder
Inference for `"stream": true` (with `stream_options.include_usage`) and
forwards each `delta.content` piece to the client as it arrives. Before, the
finished answer was sent as one chunk, so the time to the first visible token
was the whole generation.

Measured with the loopback fake in `tests/test_sonder_inference_streaming.py`
(10 tokens, 0.1 s each, through the real serve handler, bridge and gateway):
first content byte at about 1.9 s before (one chunk at the end), about 0.45 s
after (health check plus the first token).

Rules that keep the stream honest:

- **One generation streams.** Only the first bridged generation of a turn
  claims the stream. A code-gate repair, an escalation rung or any other later
  model step runs unchanged, without streaming.
- **Code is held back.** While the chat code gate is enabled
  (`SONDER_CODE_GATE`, default on), forwarding stops at the first code fence;
  the rest arrives after the gate has run, so code it would repair is never
  shown as if it stood.
- **The final answer wins.** When the turn ends, the final text is compared
  with what was forwarded. A continuation (footer-free text, a trace block,
  held-back code) is sent as the remaining delta. Anything else (a repair, an
  escalation, the web-denial guard) is sent after a visible
  `[Sonder revised this answer ...]` line, and the receipt says
  `live_stream.revised: true`. Streamed receipts also carry
  `live_stream.ttft_ms`, measured from the start of the turn's model work
  (after admission) to the first forwarded delta.
- **Disconnects cancel.** A failed delta write, or a departure noticed by the
  keep-alive writer, closes the upstream connection (Sonder Inference cancels
  the session and frees the GPU) and ends the model call with `Cancelled`.
- **Errors keep their classification.** A non-2xx status before the stream is
  mapped exactly as for a non-streaming call (a 503 `not_ready` is still
  "provably not executed" and may fall back). A `data: {"error": ...}` event
  or a stream that ends before `[DONE]` is `DependencyUnavailable`: the
  request executed and is never replayed.

Live delta chunks carry a per-turn provisional `id`; the terminal chunk keeps
the interaction id as before. Unaffected: non-streamed requests, every other
provider (including the Ollama fallback of a `sonder_inference` binding),
structured (`response_format`) turns, and routed/slash/web answers, which
still arrive as one chunk.

## Thinking (`SONDER_INFERENCE_THINKING`, default `auto`)

The bridge now carries a boolean `think` for `sonder_inference` rungs instead
of refusing `true` and dropping `false`. The gateway forwards it as
`chat_template_kwargs.enable_thinking` only when the server advertises support
in its health document: a `thinking`, `chat_template_kwargs` or
`enable_thinking` entry in a top-level or `sonder` `features`/`capabilities`
list, or in any `backends[]`/`models[]` `capabilities` list. Without that,
the historical behaviour holds (`think=true` is refused as an Ollama-only
feature, `think=false` is dropped), because a server that ignores the field
would otherwise silently think anyway.

| Value | Effect |
|---|---|
| `auto` (default) | Forward when advertised, else refuse `true` / drop `false`. |
| `on` | Always forward (the operator asserts the server honours it). |
| `off` | Never forward. |

The refusal now follows the (cached, 2 s bounded) health check instead of
preceding it; the prompt is still never sent.

## Sampling defaults (`SONDER_INFERENCE_SAMPLING_DEFAULTS`, default off)

A GGUF can carry the recommended temperature/top_p/top_k but not `min_p`, so
llama.cpp samples with `min_p` 0.05. With `SONDER_INFERENCE_SAMPLING_DEFAULTS=1`
the gateway fills a model family's recommended values for every sampling field
the caller did not set (a caller-set value is never overridden). The built-in
table has one family:

| Family (`qwen3.x`, not `coder`) | temperature | top_p | top_k | min_p | presence_penalty |
|---|---|---|---|---|---|
| thinking | 1.0 | 0.95 | 20 | 0 | 0 |
| non-thinking | 0.7 | 0.8 | 20 | 0 | 1.5 |

The row follows the forwarded `enable_thinking`; when none is forwarded the
family's template default (thinking, for Qwen3) applies. A request for the
`default` model is matched through the health document's default model id.
`SONDER_INFERENCE_SAMPLING_TABLE` replaces the table with a JSON list of
`{"family", "match", "exclude", "template_default_thinking", "thinking",
"non_thinking"}` objects (fields: `temperature`, `top_p`, `top_k`, `min_p`,
`presence_penalty`, `frequency_penalty`, `repeat_penalty`).

Off by default because it changes decoding for every tier bound to the
provider, and that has not been shown to be neutral for all of them (the chat
path always sets its own temperature, so in practice the table adds
`top_p`/`top_k`/`min_p`/`presence_penalty`).

## Telemetry

`from_openai_compatible` now also reads `timings.ttft_ms`, the prompt total
(`usage.prompt_tokens`), cached prompt tokens
(`usage.prompt_tokens_details.cached_tokens`, else `timings.cache_n`),
`timings.draft_n`/`draft_n_accepted` and the completion count. Absent or
inconsistent values stay unknown, never 0. `timings.prompt_n` is not read as
the prompt total: it means the uncached part on llama.cpp's own server. The
bridge copies cached prompt tokens into the Ollama-shaped
`prompt_eval_cached_count`, so bridged turns report prefix-cache reuse where
Ollama turns do.

## Keeping the primary model resident (`SONDER_KEEP_PRIMARY_RESIDENT`, default off)

`SONDER_KEEP_ALIVE` (default `2m`) unloads an idle Ollama model; a ~27B model
then costs ~20 s to reload at the next turn. With
`SONDER_KEEP_PRIMARY_RESIDENT=1`, requests for the primary chat model (the
model the default chat route resolves to) send `keep_alive: -1`; every other
model keeps `SONDER_KEEP_ALIVE`. Use it only when that model is the only local
model on the GPU: the `sonder_inference_gpu` doctor check warns when other
local Ollama models can load. A pinned model stays loaded until Ollama
restarts or `ollama stop <model>`, including after the primary tier changes.
This applies to models served by Ollama directly; a `sonder_inference` rung's
residency is Sonder Inference's own configuration.

## GPU contention warning (doctor `sonder_inference_gpu`)

A 15+ GB `llama-server` on a 16 GB card leaves no room: any other local model
Ollama loads onto the GPU pushes the server into shared memory and decode drops
2-15x without an error. The check (configuration only, no I/O) warns when a
tier is bound to a loopback Sonder Inference and a provider tier is bound to a
local, non-cloud Ollama model, or the Ollama embedder is not kept on the CPU
(`SONDER_EMBED_ON_CPU=1`). Nothing is changed automatically. `--skip-inference`
skips it with the other Inference checks.
