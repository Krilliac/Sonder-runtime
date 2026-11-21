# OpenAI-compatible endpoint: spend cap

The `openai_compatible` provider (`SONDER_MODEL_BACKEND=openai_compatible` or a
tier bound to it) talks to any OpenAI-compatible server. A **loopback**
endpoint is local and is never metered. A **non-loopback** endpoint (for
example `https://api.openai.com`) is metered: it needs a declared price and
counts against a local daily cap, like the Anthropic provider
(`docs/integration/anthropic.md`).

The Sonder-Inference LAN host and OpenRouter use their own gateways and are not
metered by this setting.

## Variables

| Variable | Default | Meaning |
|---|---|---|
| `SONDER_OPENAI_BASE_URL` | none | Endpoint (https unless a listed private network). |
| `SONDER_OPENAI_API_KEY` | none | Bearer key. |
| `SONDER_OPENAI_MODEL` / `SONDER_OPENAI_EMBED_MODEL` | none | Chat / embedding model ids. |
| `SONDER_OPENAI_PRICE_USD_PER_MTOK` | unset (unpriced) | `<input>,<output>` USD per 1M tokens, each 0..10000. `0,0` declares a free endpoint, for example a private LAN server. |
| `SONDER_OPENAI_EMBED_PRICE_USD_PER_MTOK` | unset (unpriced) | `<input>` USD per 1M tokens for embeddings. |
| `SONDER_OPENAI_DAILY_USD` | `10` | Local daily spend cap in USD, 0..1000000 (`0` refuses every metered call). |
| `SONDER_OPENAI_MAX_TOKENS` | `16000` | Output ceiling for metered calls (1..1000000); a request's `num_predict` is clamped to it. |

There is no built-in price table: prices differ per provider and drift, so you
state them. A metered call with no price is refused before anything is sent.

## How the cap works

- The ledger is `openai-spend.json` in the Sonder state home (per UTC day, shared
  by every process on that home). It holds dollar totals and counts only, never a
  key, a prompt or a reply.
- Before each send the call's worst case (the request's UTF-8 byte length as
  input tokens, plus `max_tokens` of output) is reserved. A call is refused with
  a message naming the cap if that would take the day over it; the refusal lasts
  until 00:00 UTC or until you raise the cap.
- The real cost is taken from the response's `usage` (`prompt_tokens`,
  `completion_tokens`). A response that reports neither is charged the worst case.
- A request that was sent and then failed in any way other than the provider
  answering with an HTTP error (timeout, connection reset, stream stall,
  cancellation, unusable body) is also charged the worst case: spend may be
  over-counted, never under-counted. An HTTP 4xx/5xx answer, and any failure
  before the send (consent, transport policy, admission, cap), is not charged.
