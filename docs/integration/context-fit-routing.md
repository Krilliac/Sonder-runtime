# Context-fit routing and Sonder Inference launch profiles

How Runtime keeps a chat turn off a model whose context window cannot hold
it: the context length it reads for each tier (Sonder Inference launch
profiles on `/v1/models`, an Ollama tier's `num_ctx`, or the Ollama model's
own metadata), the optional long-context tier a too-long turn moves to, and
the error a turn gets when no tier can hold it. Provider reference:
[sonder-inference-provider.md](../architecture/sonder-inference-provider.md);
request path: [sonder-inference-request-path.md](sonder-inference-request-path.md);
the opt-in long-context overflow to a pool model:
[configuration](../wiki/03-configuration.md#long-context-overflow).

## Bind a tier to a launch profile

A Sonder Inference launch profile is served under a model id (the profile
name). A tier binds to it the way it binds to any Sonder Inference model:

```bash
SONDER_REASONING_PROVIDER=sonder-inference
SONDER_INFERENCE_TIER_MODELS=reasoning=qwen3.8-27b-q3-100k
```

A tier without an entry uses `SONDER_INFERENCE_MODEL` (default `default`,
which resolves to the entry Inference marks `"default": true`). One Inference
`serve` hosts one profile today and Runtime takes a single Inference base URL,
so tiers that need different context lengths usually pair one Inference
profile with Ollama tiers (example below).

## Metadata Runtime reads from `/v1/models`

For each `data[]` entry Runtime reads these optional fields, first from the
entry's `sonder.profile` object (where `serve --profile` publishes them), then
from its `sonder` object, then from the entry itself:

| Field | Meaning | Also accepted as |
|---|---|---|
| `context_length` | tokens the profile serves (positive integer); after an `auto_fit` shrink Inference reports the running value here | `context_tokens`, `n_ctx` |
| `capabilities` | capability names a request to Sonder can use, e.g. `["speculative"]`; `[]` means none | |
| `profile` | launch-profile name: `sonder.profile.name`, or a plain string | |

The entry Inference publishes for a 100k-token profile (abridged):

```json
{"id": "qwen3.8-27b-q3-100k", "object": "model", "owned_by": "sonder-inference",
 "sonder": {"backend": "llamaserver", "default": true, "synthetic": false,
   "profile": {"name": "qwen3.8-27b-q3-100k", "backend": "llamaserver",
     "context_length": 100096, "cache_type_k": "q8_0", "cache_type_v": "q5_1",
     "flash_attn": "on", "capabilities": [], "upstream_capabilities": ["tools"],
     "estimated_vram_mib": 15437, "served": true}}}
```

Absent fields are reported as unknown (`null`). An Inference that publishes
none of them, or has no `/v1/models` route at all (HTTP 404), leaves every
tier's context length unknown, and an unknown context length never changes a
route: Runtime then behaves exactly as it did without profiles. The parsing
rules are pure (`sonder_runtime/domain/inference_profiles.py`,
`parse_launch_profile_models`).

Reads are bounded to 2 seconds, go straight to the configured endpoint (no
proxy, no redirects, the configured CA bundle) and are cached per endpoint
for `SONDER_INFERENCE_PROFILE_TTL_SECONDS` (default 60, range 0-3600; an
invalid value keeps the default). Failures and a missing route are cached
too, so an older Inference costs at most one short GET per TTL. A remote
endpoint is contacted only under the same consent rules as every other
Inference request. `SonderInferenceGateway.generate` never reads it.

## Where the effective context is shown

- **Provider status** (`/v1/sonder/ecosystem`, the app's provider card): the
  `sonder_inference` entry has an additive `tier_context` field,
  `{"state", "detail", "tiers": {tier: {"model", "profile", "context_length",
  "capabilities", "source"}}}`, filled while the server is ready and not busy
  (`null` otherwise). `state` is `ok`, `absent` (no `/v1/models` or no model
  list) or `error`.
- **Doctor**: the `sonder_inference` check appends
  `tier context: reasoning=100096 (qwen3.8-27b-q3-100k), ...` when the server
  publishes context lengths; otherwise its text is unchanged.
- **`runtime_policy_status`**: lists the long-context tier and every tier's
  effective context. It stays read-only (no socket), so a Sonder Inference
  tier shows the last cached profile metadata, or "unknown" until status or
  doctor has read it. An Ollama tier shows its configured `num_ctx`, or
  "auto" (sized per request from the model's metadata).

## Context-length routing

Before a chat turn runs, Runtime estimates the prompt (history plus the new
message) with the context-health estimator (about four characters per
token); there is no tokenizer. The system prompt and retrieved memory are
added later and are not counted, so the estimate is a floor.

- **At or below 4096 estimated tokens** nothing is looked up.
- **Above it**, the estimate is compared with the context length of the model
  the turn's first rung is bound to: a Sonder Inference tier's profile
  `context_length`, or an Ollama tier's configured `num_ctx` (below), else the
  model's own context length from Ollama's `/api/show` metadata (the same
  cached read the turn's automatic window already makes).

| Situation | What happens |
|---|---|
| fits, or the context length is unknown | the turn routes exactly as before |
| does not fit, long-context tier set | the turn runs on the long-context tier; the receipt (`sonder_receipt.overflow` with `route: "context_fit"`), a `route.changed` event (`reason_code` `context_exceeds_model`) and an activity entry say so |
| does not fit, no long-context tier (or it is too small, unbound, or excluded by capability evidence) | the turn fails before any model call with `ERROR: this request is about N tokens (estimated), which does not fit ...` and how to fix it; nothing is truncated |

Set the long-context tier in the runtime policy
(`runtime_policy_update routing_json='{"long_context_tier": "reasoning"}'`,
clear it with `""`) or per process with `SONDER_LONG_CONTEXT_TIER=reasoning`
(the environment wins; an invalid value is ignored and shown in
`runtime_policy_status`). It must be a local tier (`fast`, `code`, `general`,
`reasoning`, `vision`); lanes, cloud tiers and model names are refused. The
policy file stores the key only while it is set, so a policy that never used
it keeps its shape, and the shipped `default_routing` never sets it.

The rerouted turn is a single attempt: stepping back to a tier that cannot
hold the prompt would truncate it. On an Ollama long-context tier, the
request window is raised to hold the prompt plus answer room, up to that
tier's context. Exact model pins and cloud targets are never rerouted; a pin
that is known not to fit fails with the same error. A request with an
explicit context size is measured against the model's already-cached context
length only (no metadata read). Capability evidence is respected: when
routing mode is not `off` and a fresh long-context failure is recorded for
the long-context tier's Ollama model, that tier is not used.

The opt-in long-context overflow (`/runtime overflow`) is decided first; when
it switches a turn, context-fit routing does not run for that turn.

Scope: the check runs on the HTTP chat route (`POST /v1/chat/completions`,
the app). The MCP `sonder` tool, structured-output calls, agents and fleet
paths route as before.

### Example: a 100k-token profile next to an Ollama tier

A Qwen3.8-27B UD-Q3_K_XL profile served through llama-server with a
100096-token context, K cache `q8_0`, V cache `q5_1` and flash attention on
fits a 16 GB GPU (about 15.9 GB used). Serve it as the reasoning tier
through Inference, keep `code` on Ollama with a 32k window, and make
`reasoning` the long-context tier:

```bash
SONDER_REASONING_PROVIDER=sonder-inference
SONDER_INFERENCE_TIER_MODELS=reasoning=qwen3.8-27b-q3-100k
SONDER_OLLAMA_TIER_OPTIONS='{"code": {"num_ctx": 32768}}'
SONDER_LONG_CONTEXT_TIER=reasoning
```

A 40k-token conversation on `code` then runs on `reasoning` with the notice
`context fit: routed to long-context tier reasoning (qwen3.8-27b-q3-100k) --
request ~40k tokens > 32.8k context of <code model> on tier code`. A
120k-token conversation fails with the actionable error, because no tier
holds it. A 3k-token conversation on `code` is not looked at at all.

## Per-tier Ollama window

`SONDER_OLLAMA_TIER_OPTIONS` sets an Ollama tier's request window, as a JSON
object:

```bash
SONDER_OLLAMA_TIER_OPTIONS='{"reasoning": {"num_ctx": 65536}, "code": {"num_ctx": 32768}}'
```

| Option | Range | Effect |
|---|---|---|
| `num_ctx` | 512-1048576 | the tier's request window, instead of the automatic model-aware window; an explicit per-request context size (or a ModelGateway request's `num_ctx` option) still wins. It is also the context length context-fit routing measures the tier against. |

Unset, empty or invalid configuration changes nothing (an invalid value, an
unknown tier or an unknown option discards the whole value and is shown in
`runtime_policy_status`). The window applies on the HTTP chat route and to
ModelGateway consumers on Ollama tiers; cloud targets are never affected.
