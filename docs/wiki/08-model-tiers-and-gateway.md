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
`SONDER_REASONING` / `SONDER_VISION`, or point the
`sonder:latest` alias at your chosen model. The local `code` tier is
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
  `SONDER_ALLOW_REMOTE_OLLAMA=1` to point at a non-loopback Ollama origin.
- **Hosted/cloud tiers** — set `[features].cloud = true` /
  `SONDER_ALLOW_CLOUD=1`. Both are explicit consent gates; default is fully
  local. A non-Ollama frontier API would slot in as a new gateway adapter.
