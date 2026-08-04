# Model Tiers & Gateway

Sonder is model-agnostic. It routes each request to a **tier**, which
resolves to an Ollama model. Tiers and routing lanes are the main
quality/speed/privacy knobs.

## Tiers

| Tier | Default | Typical use |
|---|---|---|
| `fast` | small (3–4B) | router, titling, summaries, mechanical work |
| `code` | coder 7B | workbench, autopilot, code generation |
| `general` | 7B instruct | general chat |
| `cloud-*` | Ollama-hosted | metered, leaves the machine (consent-gated) |

Set with `SONDER_FAST` / `SONDER_CODE` / `SONDER_GENERAL`, or point the
`sonder:latest` alias at your chosen model. The local `code` tier is
memory-augmented (facts + lessons); cloud tiers answer without
augmentation but are still captured for learning.

## Routing lanes (runtime policy)

The hot-reloadable `runtime_policy.json` maps **lanes** to tiers:
`router`, `workbench`, `autopilot`, `fleet`, `review`. This lets one model
serve chat while another drives the agent loop — e.g. a 4B on `router`,
a coder-7B on `workbench`/`review`. Policy can select aliases, lanes, and
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
