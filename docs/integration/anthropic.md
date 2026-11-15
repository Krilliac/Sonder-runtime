# Anthropic (Claude API) provider

Route one or more Sonder tiers to Claude models on the Anthropic API, with a
local daily spend cap and an optional second API key.

Anthropic is a **hosted, metered** service: prompts (which in Sonder usually
contain source code and tool output) and your API key leave this machine, and
each call is billed to your Anthropic account. It is **off by default** and
stays off until you do all of: set a key, enable cloud, and bind a tier to it.
Nothing about local routing changes while it is not configured. Compare
[OpenRouter](openrouter.md), which this provider is modelled on.

Implementation: `sonder_runtime/adapters/inference/anthropic_gateway.py`
(gateway), `anthropic_spend.py` (persistent daily spend),
`sonder_runtime/domain/anthropic_policy.py` (model ids, price table, cost and
credit-error rules), `sonder_runtime/bootstrap/anthropic_cli.py` (status CLI).

## 1. Install the SDK (optional dependency)

The gateway uses the official `anthropic` Python SDK, imported lazily on the
first call. Installations that never bind a tier to Anthropic do not need it:

```powershell
pip install anthropic          # or: pip install -r requirements-anthropic.txt
```

Without it the first call fails with a typed error that says
`pip install anthropic`; the rest of the runtime is unaffected. The SDK's own
automatic retries are turned off (`max_retries=0`), so a billed request is
never silently re-sent.

## 2. Set the API key(s)

Store the key as a **user** environment variable, not in `sonder.toml` or a
committed file. On Windows:

```powershell
[Environment]::SetEnvironmentVariable('SONDER_ANTHROPIC_API_KEY', '<your key>', 'User')
[Environment]::SetEnvironmentVariable('SONDER_ANTHROPIC_API_KEY_2', '<optional second key>', 'User')
```

Then restart the terminal and Sonder. The keys are read from the process
environment at call time. **`ANTHROPIC_API_KEY` is deliberately not read**: Claude
Code and the SDK's own default client use that name, and a coding-assistant key
must never be spent by this runtime by accident.

The SDK client is built with an explicit key and an explicit base URL
(`https://api.anthropic.com`, or `SONDER_ANTHROPIC_BASE_URL`, which must be
https with no credentials, query or fragment). That makes the SDK ignore
`ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN` and `ANTHROPIC_BASE_URL` (Claude
Code sets the last one), and Sonder clears the headers the SDK would otherwise
merge from `ANTHROPIC_CUSTOM_HEADERS` and verifies the client before first use,
so a key is only ever sent to that endpoint. The key never appears in a log
line, error, status document, telemetry label, evidence record or export (both
names are in the logging redaction list and are stripped from child-process
environments); errors keep no reference to the SDK exception. Only the key
*slot* (1 or 2) that served a call is recorded. One SDK client is kept per key
slot and closed by `AnthropicGateway.close()`.

The SDK's own logging is pinned to WARNING when the client is built, whatever
`ANTHROPIC_LOG` says: `ANTHROPIC_LOG=debug` would otherwise write request
bodies (prompt text) to the `anthropic` logger and a stderr handler.

## 3. Enable cloud and bind a tier

```powershell
[Environment]::SetEnvironmentVariable('SONDER_ALLOW_CLOUD', '1', 'User')
[Environment]::SetEnvironmentVariable('SONDER_REASONING_PROVIDER', 'anthropic', 'User')
```

Every call needs `SONDER_ALLOW_CLOUD=1` **and** an operation context that allows
cloud. Nothing routes to Anthropic unless a tier is bound with
`SONDER_<TIER>_PROVIDER=anthropic` (tiers: fast, general, code, reasoning,
vision) or `SONDER_MODEL_BACKEND=anthropic`.

## 4. Variables

| Variable | Default | Meaning |
|---|---|---|
| `SONDER_ANTHROPIC_API_KEY` | none | Key slot 1 (required). |
| `SONDER_ANTHROPIC_API_KEY_2` | none | Key slot 2 (optional failover). |
| `SONDER_ANTHROPIC_MODEL` | `claude-opus-5-5` | Model for any tier without its own entry. |
| `SONDER_ANTHROPIC_TIER_MODELS` | none | `tier=model,...`, e.g. `fast=claude-haiku-5-5,reasoning=claude-fable-5-2`. |
| `SONDER_ANTHROPIC_MAX_TOKENS` | `16000` | Ceiling for `max_tokens` (a request's `num_predict` is clamped to it). |
| `SONDER_ANTHROPIC_DAILY_USD` | `10` | Local daily spend cap in USD (`0` refuses every call). |
| `SONDER_ANTHROPIC_TIMEOUT_SECONDS` | `300` | Per-call timeout, further bounded by the operation deadline. |
| `SONDER_ANTHROPIC_BASE_URL` | `https://api.anthropic.com` | Optional endpoint override; https only. |

An explicit `model` request option wins over the tier map. Only models in the
price table (`domain/anthropic_policy.py`, copied from the Claude API skill's
`shared/models.md`: Fable 5.2/5.1, Mythos 5.2/5.1, Opus 5.5, Sonnet 5.5, Haiku
5.5) are accepted, because a model with no price could not be counted against
the cap.

## 5. Key failover

Key 1 is used first. The call fails over to key 2, **at most once per call**,
only when key 1 was refused with an authentication error (HTTP 401) or a
credit-exhausted error (HTTP 400 whose message says the credit balance is too
low, or HTTP 402). A refused call was not charged, so this is not a retry of
billed work. Every other error (429, 5xx, 529, other 4xx) stays single-attempt
and never touches key 2. A 429 starts a cool-down for the key slot that was
rate limited, not for the other one. Which slot served each call is shown in
`python -m sonder_runtime anthropic status` and in the usage log line.

## 6. Daily spend cap

Each response's token usage is priced from the table and added to a ledger in
the Sonder state home (`anthropic-spend.json`, per UTC day), so the total
survives restarts and is shared by every process on that home. A call is
refused if its worst-case cost (below) would take the day over
`SONDER_ANTHROPIC_DAILY_USD`; the error names the cap and today's spend, and
lasts until 00:00 UTC or until you raise the cap. A single call whose worst case
alone exceeds the cap is refused with a message naming the cap and that worst
case (not today's spend), suggesting a lower `max_tokens`, a shorter prompt or
a higher cap; a cap of `0` says Anthropic calls are disabled. A successful
response that carries no usage block, or a usage block with neither
`input_tokens` nor `output_tokens`, is charged that worst-case amount rather
than zero; usage with at least one of them is priced from the counts present.
A call already in flight is never cut off. Before each send the gateway
reserves a worst-case cost (`max_tokens` of output plus the prompt's byte length
as input) that counts against the cap until the call finishes, so concurrent
calls cannot all pass a check made just under the cap; reservations are per
process. A stream that is abandoned, times out or fails after it started is
billed for the usage it had already consumed. Responses pinned to US-only
inference (`usage.inference_geo` of `us`) are priced at the documented 1.1x. A ledger that
cannot be read fails closed (calls are refused) rather than resetting to zero.

## 7. What it carries

System prompt, user/assistant turns, `num_predict` (as `max_tokens`) and `stop`
(as `stop_sequences`), with streaming. Like the OpenRouter gateway it returns
text only: tools, tool choice, thinking and structured-output options are
refused rather than dropped, and sampling options (temperature and friends,
which current Claude models reject) are not forwarded. Embeddings are not served.

## 8. Status (read-only)

```powershell
python -m sonder_runtime anthropic status [--json]
```

Shows whether the provider is configured, which key slots are set (booleans
only), whether the SDK is installed, and today's spend against the cap. It
reads configuration and the local ledger only; it makes no network call, and
nothing in Sonder probes the paid API automatically.

## 9. Errors

| Condition | Error |
|---|---|
| cloud opt-in off, or context forbids cloud | `Forbidden`, names `SONDER_ALLOW_CLOUD` |
| no key set | `InvalidInput`, names `SONDER_ANTHROPIC_API_KEY` |
| SDK not installed | `AnthropicSdkMissing`, says `pip install anthropic` |
| 401 | `Forbidden`, names the key slot's variable (after failover) |
| credit exhausted (400/402) | `AnthropicCreditsExhausted` (a `Forbidden`) |
| 429 | `AnthropicRateLimited`; `retry-after` becomes a local cool-down |
| 529 / 5xx | `DependencyUnavailable` |
| daily cap reached | `AnthropicDailyCapReached` (a `Forbidden`) naming cap and spend |

## 10. Live smoke test

`tests/live/test_anthropic_live_smoke.py` sends one tiny prompt with
`max_tokens` 16. It runs only with `SONDER_LIVE_ANTHROPIC=1` and a key set, and
`pytest --run-network --run-model -m 'model and network'`.
