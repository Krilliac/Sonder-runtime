# Sonder Runtime architecture

## What Sonder is

Sonder is an AI runtime and orchestration system. It is not a foundation model,
a pretrained language model, or a repository of base-model weights.

Sonder owns the behavior around a model:

- model and execution-tier selection;
- prompt construction, conversation state, memory, and retrieval;
- guarded tools, grounding, activity evidence, Autopilot, and source improvement;
- hardware detection and inference/training planning;
- attended adapter training, validation, deployment, and rollback;
- the OpenAI-compatible API, MCP server, REPL, and mobile/desktop clients.

## What Ollama does

Ollama is the local model server used by Sonder. Ollama manages model manifests
and blobs, loads the selected model weights into system RAM and GPU VRAM, runs
token inference, and returns model output to the Sonder runtime.

An Ollama name such as `sonder:latest` is an alias/model entry in Ollama's model
store. It is not a model implemented inside this repository. The default alias
can point to a supported Qwen2.5-Coder size selected for the detected hardware,
but the runtime is designed to route among compatible local or explicitly
enabled hosted models.

```text
Apps / API clients / REPL / MCP clients
                  |
                  v
Sonder Runtime
policy | memory | retrieval | tools | grounding | validation
                  |
                  v
Ollama local model server
model storage | RAM/VRAM residency | inference
                  |
                  v
Base or personal model weights
```

## Model and adapter lifecycle

1. Sonder detects system RAM, GPU runtime, VRAM, and workload settings.
2. The runtime recommends an inference model independently from a training plan.
3. Ollama installs or imports the selected inference model and serves it.
4. Normal learning stores grounded interactions, outcomes, memories, and lessons;
   this changes runtime context, not base weights.
5. Only an explicit attended training command launches QLoRA/LoRA through the
   Hugging Face Transformers, PEFT, and bitsandbytes training stack.
6. The training stack freezes the base and updates real adapter weights.
7. llama.cpp tooling converts the validated adapter to an Ollama-supported GGUF
   artifact.
8. Sonder creates and validates `sonder-personal:latest`, then updates runtime
   tiers only after loading and inference checks pass.
9. Rollback returns routing to `sonder:latest`; checkpoints and installed models
   are preserved.

Ollama serves the deployed result, but it does not perform adapter training. The
QLoRA optimization loop is supervised by Sonder Runtime and executed by the
Hugging Face/PEFT training stack.

## Storage boundaries

- Source code: this Git repository.
- Runtime state, memory, ledgers, and backups: the per-user Sonder state home.
- Base model and embedding artifacts: Ollama's model store or a separately built
  sealed engine bundle.
- Training checkpoints and adapters: the configured Sonder training output
  directory.
- Secrets: environment/configuration and private per-user state, never model
  aliases or source-controlled manifests.

See [TRAINING.md](TRAINING.md), [MOBILE_HOST_CONTROL.md](MOBILE_HOST_CONTROL.md),
and [SELFMOD.md](SELFMOD.md) for the guarded lifecycle details.
