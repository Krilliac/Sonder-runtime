# OpenRouter provider

Route one or more Sonder tiers to models hosted behind
[OpenRouter](https://openrouter.ai), see which models your account can use,
check your credits, and pick the model each tier uses.

OpenRouter is a **hosted, metered** service: prompts (which in Sonder usually
contain source code and tool output) and your API key leave this machine, and
each call is billed to your OpenRouter account. It is therefore **off by
default** and stays off until you do all three of: set a key, enable cloud,
and bind a tier to it. Nothing about local routing changes while OpenRouter is
not configured.

Implementation: `sonder_runtime/adapters/inference/openrouter_gateway.py`
(gateway, discovery), `sonder_runtime/domain/openrouter_policy.py` (model id
grammar, routing preferences), `sonder_runtime/bootstrap/openrouter_cli.py`
(CLI), `sonder_runtime/bootstrap/openrouter_tools.py` (MCP tools).

## 1. Set the API key

Create a key at <https://openrouter.ai/settings/keys> and store it as a
**user** environment variable named `OPENROUTER_API_KEY`. On Windows:

```powershell
[Environment]::SetEnvironmentVariable('OPENROUTER_API_KEY', '<your key>', 'User')
```

Then restart the terminal and Sonder (running processes do not see new user
variables). The gateway reads the key from the process environment at call
time; never put it in `sonder.toml` or a committed file.

Sonder sends the key only as the `Authorization: Bearer` header to
`https://openrouter.ai/api/v1`. It never logs, prints, stores or exports it,
strips it from child-process environments, and redacts `sk-or-...` values
from logs and error text. A missing key produces an error that names
`OPENROUTER_API_KEY` (never a value).

## 2. Enable cloud

Every OpenRouter call, including the read-only model listing and account
checks, requires the explicit cloud opt-in:

```powershell
$env:SONDER_ALLOW_CLOUD = '1'      # or set it as a user variable for a durable opt-in
```

Inside a running MCP session, `cloud_opt_in on` enables it for that process
only. Prompt-bearing calls additionally require an operation context that
allows cloud; a context that does not is refused before anything is sent.

## 3. Find models and check credits

```powershell
python -m sonder_runtime openrouter account
python -m sonder_runtime openrouter models
python -m sonder_runtime openrouter models --search claude --tools
python -m sonder_runtime openrouter models --json
```

`models` lists the models available to your key (OpenRouter's account-filtered
listing, which honours your provider and privacy settings on openrouter.ai;
without a key it falls back to the public catalog and says so). Columns: id,
context length, USD per 1M prompt tokens, USD per 1M completion tokens, and
whether the model supports tool calling and structured outputs.

`account` shows the key's spending limit, remaining limit and usage (total,
today, this week, this month), plus the account credit balance when the key
is allowed to read it (OpenRouter serves `/credits` to management keys only;
otherwise the note says so).

Both are free, read-only calls: they send the key, never a prompt. MCP
clients get the same information from the `openrouter_models` and
`openrouter_account` tools (graded `safe`, like other network reads; they
refuse while cloud is off).

## 4. Pick a model per tier

```powershell
python -m sonder_runtime openrouter use code anthropic/claude-sonnet-4
python -m sonder_runtime openrouter use reasoning deepseek/deepseek-r1 --verify
python -m sonder_runtime openrouter use code none      # clear the mapping
```

Tiers are `fast`, `general`, `code`, `reasoning`, `vision`. Model ids must look
like `vendor/model` or `vendor/model:variant` (for example
`meta-llama/llama-3.3-70b-instruct:free`). `--verify` first confirms the id
is listed for your key.

`use` writes the `provider_models.openrouter` section of the shared runtime
policy through the same guarded update path as `/runtime set` (cross-process
lock, revision bump, and refusal while a model deployment transition is
active). The runtime policy still never accepts cloud models for the local
Ollama tiers and cannot bind a tier to OpenRouter, supply a key or enable
cloud: those stay in host configuration. The mapping is operator-only; no
model-facing tool can change it.

Model precedence for a request: an explicit `model` request option, then
`SONDER_OPENROUTER_TIER_MODELS` (for example
`code=openai/gpt-5-mini,fast=google/gemini-2.5-flash`), then the policy
mapping from `openrouter use`, then `SONDER_OPENROUTER_MODEL`.

## 5. Route tiers to OpenRouter

Bind the tiers you want (others stay on local Ollama):

```powershell
$env:SONDER_CODE_PROVIDER = 'openrouter'
$env:SONDER_REASONING_PROVIDER = 'openrouter'
```

A tier bound to OpenRouter gets the same hosted-data boundary as a
`cloud-*` tier: its requests carry only request-scoped instructions and the
runtime identity, never the local profile, emotion vectors, active goal, or
recalled memory, lessons and facts.

`SONDER_MODEL_BACKEND=openrouter` binds every tier. OpenRouter does not serve
embeddings through this gateway, so keep `SONDER_EMBEDDING_PROVIDER=ollama`
(the default when only tiers are bound). `python -m sonder_runtime preflight`
reports an `openrouter` check (configuration only; it never calls the paid
API).

## Privacy defaults and provider routing

Every chat request carries OpenRouter's `provider` routing object. The
defaults are privacy-first because Sonder prompts are code-heavy:

| Field | Default | Meaning |
|---|---|---|
| `data_collection` | `"deny"` | only upstream hosts that do not store or train on prompts |
| `zdr` | `true` | only zero-data-retention endpoints |
| `allow_fallbacks` | `true` | OpenRouter may fail over between eligible hosts |

Some models have no endpoint that satisfies `zdr: true`; such a request fails
with a clear "no endpoint ... relax the provider preferences" error. Turn the
restriction off explicitly, per tier if possible.

Overrides, applied in this order over the defaults (each replaces whole keys):

- `SONDER_OPENROUTER_PROVIDER`: a JSON object for every tier, e.g.
  `{"sort": "throughput", "require_parameters": true}`.
- `SONDER_OPENROUTER_PROVIDER_ORDER`: a preferred host order, e.g.
  `deepinfra,fireworks` (becomes `"order": ["deepinfra", "fireworks"]`).
- `SONDER_OPENROUTER_TIER_PROVIDER`: a JSON object keyed by tier, e.g.
  `{"reasoning": {"zdr": false, "only": ["deepinfra"]}}`.

Accepted keys (unknown keys are refused so a typo cannot drop a privacy
setting): `order`, `allow_fallbacks`, `require_parameters`,
`data_collection`, `zdr`, `enforce_distillable_text`, `only`, `ignore`,
`quantizations`, `sort`, `preferred_min_throughput`,
`preferred_max_latency`, `max_price`.

Optional app attribution headers are **off** by default; set
`SONDER_OPENROUTER_APP_URL` (sent as `HTTP-Referer`) and/or
`SONDER_OPENROUTER_APP_TITLE` (sent as `X-Title`) to appear in OpenRouter's
app rankings.

## Costs and telemetry

OpenRouter reports the cost of every call (`usage.cost`, USD) and token
details. Sonder records them per call with `backend="openrouter"` and the
upstream host OpenRouter names:

- `sonder_model_cost_usd_total{backend,upstream}`
- `sonder_model_usage_tokens_total{backend,upstream,kind}` with kinds
  `prompt`, `completion`, `cached`, `cache_write`, `reasoning`
- the existing `sonder_model_prompt_tokens{backend,state}` histogram
  (total / cached / uncached)
- one content-free `openrouter usage:` INFO log line per call

Upstream labels are bounded (lowercase slugs, at most 32 distinct values per
process, then `other`). Watch spend with `openrouter account`, and set a
spending limit on the key at openrouter.ai.

## Errors

| HTTP | Sonder error | What to do |
|---|---|---|
| 401 | `Forbidden` naming `OPENROUTER_API_KEY` | check or replace the key |
| 402 | `OpenRouterCreditsExhausted` ("insufficient OpenRouter credits") | add credits, lower max tokens, or use a cheaper / `:free` model |
| 403 | `Forbidden` | permission, guardrail or moderation refusal |
| 404 | `InvalidInput` | unknown model, or no endpoint matches the provider preferences |
| 429 | `OpenRouterRateLimited` (`retry_after` seconds) | wait; while a `Retry-After` delay is running, further sends are refused locally |
| 502/503/5xx | `DependencyUnavailable` | upstream outage or no eligible host; retry later or relax preferences |

Calls are single-attempt: Sonder never silently retries metered work.

## Conformance probes

OpenRouter is OpenAI-compatible, so the existing probes can run against it,
but never automatically (it is a paid API). To attest a route explicitly:

```powershell
python scripts/backend_attest.py --backend openrouter --model anthropic/claude-sonnet-4 --allow-cloud --dry-run
```

Drop `--dry-run` to spend a few requests on the check. The probe requests
carry the same privacy-first `provider` object.

## Configuration reference

| Variable | Purpose |
|---|---|
| `OPENROUTER_API_KEY` | API key (secret; a user environment variable) |
| `SONDER_ALLOW_CLOUD` | must be `1` for any OpenRouter call |
| `SONDER_<TIER>_PROVIDER` / `SONDER_MODEL_BACKEND` | `openrouter` (alias `open-router`) routes tiers to it |
| `SONDER_OPENROUTER_MODEL` | default model for tiers without a mapping |
| `SONDER_OPENROUTER_TIER_MODELS` | `tier=vendor/model,...`; overrides the policy mapping |
| `SONDER_OPENROUTER_PROVIDER` | JSON `provider` preferences for every tier |
| `SONDER_OPENROUTER_PROVIDER_ORDER` | comma-separated preferred host order |
| `SONDER_OPENROUTER_TIER_PROVIDER` | JSON `{tier: provider preferences}` |
| `SONDER_OPENROUTER_TIMEOUT_SECONDS` | per-call timeout (default 300) |
| `SONDER_OPENROUTER_APP_URL` / `SONDER_OPENROUTER_APP_TITLE` | optional attribution headers |
| `SONDER_OPENROUTER_BASE_URL` | testing only: defaults to `https://openrouter.ai/api/v1`; https required except for a loopback test server |
