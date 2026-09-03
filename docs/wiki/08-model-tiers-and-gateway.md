# Model Tiers & Gateway

Sonder is model-agnostic. It routes each request to a **tier**, which
resolves to an Ollama model. Tiers and routing lanes are the main
quality/speed/privacy knobs.

## Tiers

| Tier | Default | Typical use |
|---|---|---|
| `fast` | `sonder:latest` | router, titling, summaries, mechanical work |
| `code` | `sonder:latest` | workbench, autopilot, code generation |
| `general` | `sonder:latest` | general chat |
| `reasoning` | unbound | optional proofs, derivations, "think carefully" work |
| `vision` | unbound | optional images, screenshots, diagrams, OCR |
| `cloud-*` | Ollama-hosted | metered, leaves the machine (consent-gated) |

Set with `SONDER_FAST` / `SONDER_CODE` / `SONDER_GENERAL` /
`SONDER_REASONING` / `SONDER_VISION` — which seed the policy file the first
time it is created — or with `/runtime set` once it exists, or point the
`sonder:latest` alias at your chosen model.
([Model Requirements & Onboarding](19-model-requirements-and-onboarding.md)
covers verification, selection, and absent-model behavior end to end.) The local `code` tier is
memory-augmented (facts + lessons); cloud tiers answer without
augmentation but are still captured for learning.

`fast`, `code`, and `general` are **base tiers** — always bound to the stable
alias created from the model selected for this host, and the
fallback floor for everything else. `reasoning` and `vision` are **specialist
tiers**: unbound by default; an operator may bind either to an installed model
(`SONDER_VISION=none`, or `/runtime set vision=` with an empty value). An
unset tier is not offered at all — it disappears from `/v1/models` and from
`_serve_target`, and the capability router degrades that work to a base tier.

## Capability routing

The pure rules in `sonder_runtime/domain/routing/capability_router.py`
classify each request (`simple`, `search`, `code`, `reasoning`, `vision`,
`long_context`) and prefer a tier per class. This only ever *upgrades* a
lane-selected tier to a specialist tier the operator has actually bound; it
never selects cloud and never widens a permission. With only the base tiers
bound it is a deliberate no-op.

When a caller supplies the measured hardware capability report and an exact
quantized model profile, the same pure route planner also carries a
`memory_mode`: `gpu-resident`, `gpu+ram-hybrid`, or `cpu-fallback`. This is
metadata for scheduling and diagnostics; it does not silently replace the
configured model, widen permissions, or turn unknown backend readiness into a
claim of acceleration. The route remains local by default and keeps its
existing bounded escalation ladder.

Modality jobs (vision, OCR, speech, text-to-speech, and embeddings) share a
small pure scheduler that orders by priority against measured free VRAM. It
uses CPU fallback when permitted, defers work when fallback is disabled, and
refuses non-local work unless the caller explicitly supplies cloud consent.
The scheduler does not move payloads or claim that a provider is installed;
the owning gateway still performs the actual local capability check.

## Automatic escalation (default route)

The default route escalates on its own, bounded, and only upward. When a
chat turn (the MCP/REPL tool or the served `model: sonder`) or a workbench
agent run started on the default route fails on its first model, the runtime
reruns it on the next *distinct* bound local model of the capability
router's ladder for that task class, at most `MAX_ESCALATIONS` (2) times,
the same ceiling the model-gateway escalation policy enforces. The planning
lives in `sonder_runtime/application/routing/tier_escalation.py`; the loops
are in `_sonder_impl_serialized`, `_answer_with_history_impl` and
`_workbench_agent_escalating` (`server.py`).

What counts as a failure is objective: a transport failure that is not a
cancellation or an exhausted budget, an empty answer, or (for the agent) a
model that could not produce a parseable tool decision after the format
repairs, or a completion claim that changed nothing and ran no validation
on a request that asked for a change or a check (decided from the request's
action verbs, so a read or an explanation never escalates on that ground).
A satisfied answer never spends a stronger model; any other finished agent
run stands however it judged its own work. An empty attempt's captured
interaction is discarded so it never reaches lessons.

The rungs are distinct by model: two tiers bound to the same alias are one
rung, so with every tier on one model (the default policy) the plan is a
single rung and behaviour is exactly what it was. Escalation only moves up
by role (`fast` is never a rung after a `code` start; `reasoning` sits above
the working tiers), the vision tier is only a rung for a request carrying an
image, and cloud tiers are never rungs, so the privacy contract of the
default route is unchanged. A reasoning-class prompt starts on a bound
`reasoning` tier when it resolves to a different model, keeping the default
route as its fall-back. The default route's augmentation (facts, lessons,
recall) travels with the prompt to every rung.

An explicit tier, an exact model pin, or any explicit OpenAI `model` field is
a routing contract and never moves. Each escalation is recorded on the
activity record as a `model_escalation` event (`chat: sonder (a) -> general
(b): failed`), the served receipt names the target that answered, and an
escalated agent output carries a `model escalation:` line.
`SONDER_MODEL_ESCALATION=0` turns the behaviour off.

## Routing lanes (runtime policy)

The hot-reloadable `runtime_policy.json` maps **lanes** to tiers:
`router`, `workbench`, `autopilot`, `fleet`, `review`. This lets one model
serve chat while another drives the agent loop — e.g. a 4B on `router`,
a coder-7B on `workbench`/`review`. Lanes pin to **base tiers only**, so a
lane always resolves to a bound model; specialist tiers are chosen per
request by the capability router. Policy can select aliases, lanes, and
NPU modes but **cannot** widen network/filesystem/credential/cloud
permissions ([Security Model](09-security-model.md)).

## The ModelGateway port (SPEC-3)

All model transport is moving behind a single port,
`application/ports/model_gateway.py`, implemented by
`adapters/ollama/gateway.py`. The port boundary:

- enforces the **cloud-consent gate** against the request's
  `OperationContext` — a cloud tier is refused unless consent is present,
  before anything leaves the machine;
- maps transport errors into the **domain taxonomy** (timeout →
  `DeadlineExceeded`, transient → `DependencyUnavailable`, etc.), so
  callers never see driver exceptions;
- threads deadlines and cancellation into the transport; keeps local
  retries bounded and remote/hosted calls single-attempt.

Session summarization and titling already route through it
(`ChatService` → `OllamaGateway`); more call-sites migrate incrementally.

### Backend selection and typed capability metadata

`SONDER_MODEL_BACKEND` (default `ollama`) picks the transport constructed by
`adapters/model_gateway_factory.py`: unset/`ollama` builds `OllamaGateway`;
`openai`, `openai-compatible`, `llamacpp`, or `vllm` build
`OpenAICompatibleGateway`. An unrecognized value raises `InvalidInput`
instead of silently falling back to Ollama — a typo in the operator's
configuration must not route requests through a different transport than
intended.

The read-only hardware profile also inventories local provider presence for
Ollama, llama.cpp, vLLM, and TensorRT-LLM using bounded executable/package
checks. This is intentionally separate from provider readiness: finding an
executable never claims that its endpoint is healthy, that CUDA is usable, or
that a model fits in VRAM. The profile reports an advisory backend selection
for the model format and measured CUDA state; the owning gateway must still
perform its normal health, permission, and residency checks before dispatch.

Each gateway exposes a static `.capabilities` property (a `frozenset[str]`
drawn from `domain/model_capabilities.py`'s `KNOWN_GATEWAY_CAPABILITIES`
vocabulary — the same shape `ProviderHealth.capabilities` accepts). It is
never a live probe result, only a fact about the adapter's own shape:
`OllamaGateway` advertises `tiered-routing` because it resolves model
identity per request (a tier may select a different local or hosted model
each call); `OpenAICompatibleGateway` advertises `fixed-endpoint` because one
configured endpoint and model serve every request.

### Measured inference telemetry

`ModelResponse.telemetry` optionally carries backend-measured phase data:
total, model load, prompt evaluation, generation, and prompt/output token
rates. Ollama's nanosecond fields and the bounded llama.cpp-compatible
`timings` extension are normalized to milliseconds. Existing response fields
and positional constructors remain compatible; callers that do not need
telemetry can ignore it.

Missing values stay missing. Sonder does not estimate timing from response
length, and it derives a token rate only when both a backend token count and a
measured phase duration exist. A load duration alone is not called a cold
start: `load_state` is `cold` or `warm` only when the backend explicitly says
so. This follows the useful measurement discipline demonstrated by
`kimi-k3-in-c`: separate one-time load/prompt work from steady generation and
report measured facts instead of smoothing unlike phases together.

The optional Prometheus projection uses fixed, content-free labels only:
`sonder_model_backend_phase_duration_seconds{backend,phase}`,
`sonder_model_token_throughput_per_second{backend,direction}`, and
`sonder_model_load_states_total{backend,state}`. Prompts, generated text,
model names, endpoints, and arbitrary provider keys are never labels. Invalid,
negative, non-finite, or implausibly unbounded provider values are discarded.

## Choosing a model

Grounded in the live A/B runs on this codebase:

| Model size | Agent loop | Instruction following | Speed (CPU) |
|---|---|---|---|
| 1.5B | breaks mid-run | weak | fastest |
| 4B (e.g. facts.) | mostly holds | decent | fast |
| 7B coder | reliable | good | slower |

The runtime layer is identical across all of them; **output quality
scales with the model** and the agentic surfaces only become dependable
once the model can hold the tool protocol.

## Scaling up: large local models on serious hardware

Sonder is deliberately built to scale *up* as well as down. The same
runtime that imports a 4B off a USB will drive a large local model —
70B/120B, a big MoE, a multi-hundred-B or trillion-parameter model — served
by Ollama on a workstation or server with the VRAM to hold it. Nothing in
the orchestration assumes a small or weak model; the "small local model
only" gates apply specifically to the *automatic router's* self-selection,
not to the tier you point a lane at.

To make a big local model the primary reasoning tier:

```bash
# Point the heavy lanes at the large local model (developer/admin auth).
/runtime set general=<big-local-model> workbench=general review=general
# Keep the heavy model resident so its long load isn't paid per request.
export OLLAMA_KEEP_ALIVE=2h
# Give it the context its weights support (native, not just virtual).
export SONDER_NATIVE_CONTEXT_MAX=131072   # or higher, to the model's limit
```

A large local model is still **local**: it does not trip the cloud consent
gate. It is the intended way to get frontier-class reasoning while keeping
prompts, code, and memory on hardware you own — the ownership thesis without
the small-model capability tax.

Two subsystems specifically pay off in this regime:

- **Speculative execution** engages automatically here. Its adaptive cost
  model hides `min(decision, tool)` wall time per step; on big-model +
  slow-tool hardware that is real seconds, where on a laptop it correctly
  stays dormant ([Speculation & Prediction](11-speculation-and-prediction.md)).
- **Prewarm / keep-resident** matters more the larger the model, because
  cold-load latency grows with size; prewarm overlaps it with host work and
  `OLLAMA_KEEP_ALIVE` avoids re-paying it.

A large local model can also serve as the grounded **teacher tier** whose
good outcomes are distilled into lessons and fine-tuning data for a smaller,
faster local model to retrieve later ([Memory & Learning](06-memory-and-learning.md),
[Training](15-training.md)) — big iron trains the model you run everywhere else.

## Portable & offline models (facts. USB)

Any open-weight `.gguf` works. Import one from a mounted USB or a file:

```bash
python setup_alias.py --from-usb                 # discover + import
python setup_alias.py --gguf /path/to/model.gguf # pin a specific file
```

This writes an Ollama `FROM <gguf>` Modelfile (Ollama copies the weights
into its store), aliases it as `sonder:latest`, and pulls the embedding
model if reachable. Full walkthrough, including the abliteration/guardrail
note: [use-facts-model](../runbooks/use-facts-model.md).

## Remote & cloud (opt-in)

- **Remote Ollama** — set `[ollama].allow_remote = true` /
  `SONDER_ALLOW_REMOTE_OLLAMA=1` to point at a non-loopback **HTTPS** Ollama
  origin. HTTP remains available only for loopback Ollama because remote
  prompts and embeddings must be protected in transit.
- **Hosted/cloud tiers** — set `[features].cloud = true` /
  `SONDER_ALLOW_CLOUD=1`. Both are explicit consent gates; default is fully
  local. A non-Ollama frontier API would slot in as a new gateway adapter.
- **Private-node compute** — independently set `[compute].allow_remote = true`
  (or `SONDER_ALLOW_REMOTE_COMPUTE=1`) and opt in on each workload request.
  This schedules whole build/test/media/training jobs and does not enable a
  cloud tier or replace the inference gateway. See
  [Private Compute Fabric](../runbooks/compute-fabric.md).
