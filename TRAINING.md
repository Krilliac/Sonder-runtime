# Adaptive weight training in Sonder Runtime

Sonder Runtime can orchestrate real local LoRA adapter training, but Sonder is
not a foundation model and does not contain base-model weights. The components
have separate responsibilities:

- **Ollama** is the local model server and inference host. It stores model
  artifacts, loads base or deployed weights into RAM/VRAM, and runs generation.
- **Hugging Face Transformers, PEFT, and bitsandbytes** load a matching frozen
  base model for training and update the LoRA adapter weights. The adapter is a
  genuinely trained artifact even though the base weights remain frozen.
- **Sonder Runtime** provides orchestration, memory, tools, grounding, and
  policy; exports grounded training data; detects hardware; supervises the
  attended training plan; records manifests/checkpoints; validates conversion
  and inference; and controls deployment and rollback.

Training is never started by bootstrap, application startup, Autopilot, cron,
or a fleet. The first and every subsequent run is an explicit, foreground,
attended command.

## Architecture

`system_profile.detect_hardware()` reports total/available system RAM and GPU
vendor/runtime/name/VRAM/compute capability. `adaptive_training.build_plan()`
keeps VRAM and RAM as separate budgets, recommends inference independently from
training, and defaults to QLoRA with 4-bit NF4 base weights, bf16/fp16 compute,
gradient checkpointing, batch 1, and gradient accumulation 8.

The 1.5B, 3B, and 7B choices are starting ranges followed by explicit memory
estimates. Sequence length, batch size, context length, live free memory, and OS
reserves change the final decision. Full-parameter
training is an advanced explicit *feasibility report* and is rejected unless
its bf16 model, gradients, optimizer state, activations, and RAM headroom fit.
The attended local start/deploy lifecycle intentionally remains QLoRA-only.

## Commands

Inside the Sonder Runtime REPL:

```text
/hardware
/training plan --dry-run
/training plan --model auto --sequence-length 1024 --batch-size 1
/training start --confirm
/training start --confirm --resume
/training status
/training deploy --llama-cpp /path/to/llama.cpp
/training rollback
```

The same lifecycle is available without the REPL:

```bash
python adaptive_training.py hardware
python adaptive_training.py plan --dry-run --model auto
python export_training_data.py
python adaptive_training.py start --confirm --model auto
python adaptive_training.py start --confirm --resume --model auto
python adaptive_training.py status
python adaptive_training.py deploy --llama-cpp /path/to/llama.cpp
python adaptive_training.py rollback
```

Planning options include `--model auto|1.5b|3b|7b`,
`--allow-cpu-offload`, `--max-vram`, `--max-system-ram`,
`--context-length`, `--sequence-length`, `--batch-size`, `--gpu-index`, and
`--gradient-accumulation`. Corresponding `SONDER_*` environment overrides
remain supported; see `adaptive_training.py` for the exact names.

Every confirmed start creates a unique run directory under
`sonder-personal-lora/runs/<run-id>/`. Its plan records the exact base, adapter
path, selected physical GPU, and SHA-256 of the approved dataset. The controller
passes a fresh five-minute, one-use launch capability to `qlora_train.py`; direct
script invocation and replay are rejected before heavyweight ML imports. Set
`--gpu-index` to bind the physical CUDA device before Torch initializes.

`--allow-cpu-offload` is retained as an explicit capability request, but the
current bitsandbytes/Trainer backend rejects it. Hugging Face documents the
available `device_map="auto"` mechanism as an inference-only path, so Sonder
Runtime does not present it as safe QLoRA training or silently attempt it.
Select a smaller GPU-resident plan instead. CPU offload can be enabled in a
future backend only after that backend has a supported implementation and
attended validation coverage.

Training dependencies are intentionally separate:

```bash
python -m pip install -r requirements-train.txt
```

On native Windows, WSL2 remains the more reliable bitsandbytes environment.
Sonder Runtime refuses silent CPU training and stops CUDA OOM failures with
checkpoints intact. Resume is never inferred from a shared output folder: use
`start --confirm --resume` to reauthorize the recorded interrupted/failed run.
The base model, sequence length, batch size, gradient accumulation, and GPU must
match that run's manifest before its checkpoints can be resumed.

## Qwen adapter deployment

Ollama documents direct Safetensors adapters only for selected architectures;
Qwen is not in that list. Sonder Runtime therefore never assumes raw PEFT
Safetensors will load. Deployment requires a current llama.cpp checkout and
runs `convert_lora_to_gguf.py`, explicitly pinning the exact Hugging Face base
model ID recorded by PEFT and Sonder Runtime's training manifest. PEFT and
Hugging Face perform adapter training first; llama.cpp converts the result to
GGUF; Ollama then stores and serves the validated deployment artifact.

Deployment then:

1. Checks adapter/manifest/base identity and disk capacity.
2. Converts the PEFT adapter to an F16 GGUF adapter.
3. Creates a temporary model in Ollama using the exactly mapped
   `qwen2.5-coder:1.5b`, `:3b`, or `:7b` base.
4. Requires candidate inference to return the exact validation marker.
5. Preserves any existing personal alias, promotes the validated timestamped
   candidate with `ollama cp`, and validates `sonder-personal:latest` again.
6. Restores the previous personal alias if final validation or policy activation
   fails.
7. Only after both validations pass, updates Sonder Runtime's `code` and
   `general` inference tiers.

`sonder:latest` is never overwritten or deleted. Existing 1.5B/3B/7B
models, adapters, converted files, and checkpoints are also never deleted.

## Rollback

Run:

```text
/training rollback
```

or:

```bash
python adaptive_training.py rollback
```

This atomically points both Sonder Runtime tiers, `code` and `general`, back to
`sonder:latest`. It intentionally leaves the personal model and all
training artifacts in place for diagnosis or redeployment.
