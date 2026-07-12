---
base_model: Qwen/Qwen2.5-Coder-1.5B-Instruct
library_name: peft
pipeline_tag: text-generation
tags:
- adapter
- lora
- peft
- qwen2.5-coder
---

# Personal LoRA adapter for Sonder Runtime

This directory contains a PEFT LoRA adapter trained against
`Qwen/Qwen2.5-Coder-1.5B-Instruct`. It is **not Sonder Runtime**, a standalone
language model, a complete set of base-model weights, or an Ollama-ready model.

Sonder Runtime is the orchestration software around inference: it selects a
compatible model, manages memory and tools, prepares grounded prompts, plans
training, validates deployment, and controls rollback. The adapter in this
directory changes only the selected model's adapter weights.

## Exact base-model requirement

The adapter must be used with the exact base model recorded in
`adapter_config.json`:

`Qwen/Qwen2.5-Coder-1.5B-Instruct`

Loading it against a different repository, model size, architecture, or
revision is unsupported. Raw PEFT Safetensors must not be assumed to work
directly in Ollama.

## Adapter configuration

- Task: causal language modeling
- Method: LoRA/PEFT
- Rank: 16
- Alpha: 32
- Dropout: 0.05
- Bias: none
- Target modules: `q_proj`, `k_proj`, `v_proj`, `o_proj`,
  `gate_proj`, `up_proj`, and `down_proj`

The matching base weights remain separate and are not included here.

## Intended deployment

Use this artifact only through Sonder Runtime's attended training deployment
flow. That flow verifies the adapter/base identity, converts the adapter to a
supported GGUF representation when required, creates a candidate Ollama model,
runs loading and inference validation, and only then may activate
`sonder-personal:latest`.

The stable `sonder:latest` Ollama alias remains the rollback entry and is not
overwritten by this adapter.

## Limitations and validation status

- This adapter cannot generate text without its exact base model.
- Presence in the repository does not mean it is currently deployed.
- No held-out evaluation result is asserted by this card.
- Treat it as an undeployed candidate until the current runtime validation and
  health checks pass.
- Training data is not included; personal data must remain private and outside
  source control.
- All limitations and biases of the Qwen2.5-Coder base model still apply.

See `TRAINING.md` in the repository for conversion, validation, deployment,
and rollback requirements.

## Framework version

- PEFT 0.13.2
