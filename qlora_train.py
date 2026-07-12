"""QLoRA fine-tune of trilobite's base model on its own good-outcome examples.

⚠️  NOT RUN. This script has been prepared but never executed. It needs an
    ATTENDED first run — watch VRAM usage, watch for OOM, be ready to Ctrl-C.
    Do NOT launch it unattended (e.g. from a fleet/cron/loop).

What this does: reads training_data.jsonl (chat-format {"messages":[user,
assistant]} pairs exported by export_training_data.py), formats each example
with the Qwen chat template, masks the prompt so loss is only computed on the
assistant span, and QLoRA-fine-tunes a small Qwen2.5-Coder base in 4-bit (NF4)
with a LoRA adapter on top. The adapter is saved to
./trilobite-personal-lora/ by default.

Use ``adaptive_training.py plan`` (or ``/training plan``) to select the model
and memory strategy. Direct invocation retains a conservative 1.5B default,
but it will never silently fall back to CPU training.

Usage (after `pip install -r requirements-train.txt`):
    ./venv/Scripts/python.exe qlora_train.py
    TRILOBITE_BASE=Qwen/Qwen2.5-Coder-7B-Instruct ./venv/Scripts/python.exe qlora_train.py

This file must remain importable/py_compile-able WITHOUT torch/transformers/
peft/bitsandbytes installed — all heavy imports are deferred into main().
"""
import json
import os
import sys
import time

# ---------------------------------------------------------------------------
# Configuration (override via env vars; kept as simple constants on purpose)
# ---------------------------------------------------------------------------

# Direct use defaults to 1.5B; the lifecycle manager supplies its planned base.
BASE = os.environ.get("TRILOBITE_BASE", "Qwen/Qwen2.5-Coder-1.5B-Instruct")

DATA_PATH = os.environ.get("TRILOBITE_DATA", os.path.join(os.path.dirname(__file__), "training_data.jsonl"))
OUTPUT_DIR = os.environ.get("TRILOBITE_LORA_OUT", os.path.join(os.path.dirname(__file__), "trilobite-personal-lora"))

MAX_LEN = int(os.environ.get("TRILOBITE_MAX_LEN", "1024"))

# LoRA hyperparameters
LORA_R = int(os.environ.get("TRILOBITE_LORA_R", "16"))
LORA_ALPHA = int(os.environ.get("TRILOBITE_LORA_ALPHA", "32"))
LORA_DROPOUT = float(os.environ.get("TRILOBITE_LORA_DROPOUT", "0.05"))
# Attention + MLP projection modules for the Qwen2 architecture.
LORA_TARGET_MODULES = [
    "q_proj", "k_proj", "v_proj", "o_proj",       # attention
    "gate_proj", "up_proj", "down_proj",           # MLP
]

# Conservative defaults; the lifecycle manager supplies its planned values.
PER_DEVICE_BATCH_SIZE = int(os.environ.get("TRILOBITE_BATCH_SIZE", "1"))
GRAD_ACCUM_STEPS = int(os.environ.get("TRILOBITE_GRAD_ACCUM", "8"))
NUM_EPOCHS = float(os.environ.get("TRILOBITE_EPOCHS", "3"))
LEARNING_RATE = float(os.environ.get("TRILOBITE_LR", "2e-4"))
LR_SCHEDULER = "cosine"
WARMUP_RATIO = 0.03
LOGGING_STEPS = 5
SAVE_STRATEGY = "epoch"
SEED = 42


def _print_vram_guidance():
    print("=" * 78)
    print("trilobite QLoRA training — attended run required")
    print("=" * 78)
    print(f"Base model : {BASE}")
    print(f"Data       : {DATA_PATH}")
    print(f"Output     : {OUTPUT_DIR}")
    print()
    print("The hardware planner selected this configuration. Keep the first run")
    print("attended, watch `nvidia-smi` / Task Manager, and be ready to Ctrl-C.")
    print("=" * 78)


def load_examples(path):
    """Read training_data.jsonl -> list of {"messages": [user, assistant]}."""
    examples = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            msgs = obj.get("messages")
            if not msgs or len(msgs) < 2:
                continue
            examples.append(obj)
    return examples


def build_supervised_example(tokenizer, messages, max_len):
    """Format one {"messages": [...]} example with the Qwen chat template and
    tokenize it, masking everything except the final assistant turn's tokens
    (labels = -100 for prompt tokens, real ids for the assistant span).

    Returns a dict with input_ids, attention_mask, labels (python lists).
    """
    # Prompt = everything up to (not including) the assistant reply, with the
    # generation prompt appended so the template matches inference exactly.
    prompt_ids = tokenizer.apply_chat_template(
        messages[:-1],
        tokenize=True,
        add_generation_prompt=True,
    )
    full_ids = tokenizer.apply_chat_template(
        messages,
        tokenize=True,
        add_generation_prompt=False,
    )

    # Guard against templates that don't cleanly prefix (defensive, keeps
    # masking correct even if apply_chat_template output isn't a strict
    # prefix relationship for some edge case).
    prompt_len = min(len(prompt_ids), len(full_ids))

    full_ids = full_ids[:max_len]
    labels = list(full_ids)
    for i in range(min(prompt_len, len(labels))):
        labels[i] = -100

    attention_mask = [1] * len(full_ids)
    return {
        "input_ids": full_ids,
        "attention_mask": attention_mask,
        "labels": labels,
    }


def main():
    _print_vram_guidance()

    if not os.path.exists(DATA_PATH):
        print(f"ERROR: training data not found at {DATA_PATH}")
        print("Run: ./venv/Scripts/python.exe export_training_data.py")
        sys.exit(1)

    examples = load_examples(DATA_PATH)
    print(f"Loaded {len(examples)} training examples from {DATA_PATH}")
    if len(examples) == 0:
        print("ERROR: no examples to train on. Run export_training_data.py first"
              " (and use trilobite for a while so it has good-outcome interactions).")
        sys.exit(1)
    if len(examples) < 20:
        print(f"WARNING: only {len(examples)} examples — this is a proof-of-pipeline")
        print("         run, not a run that should meaningfully move model behavior.")

    # --- heavy imports, deferred so this file stays py_compile-able without
    # the training deps installed ---------------------------------------
    try:
        import torch
        from datasets import Dataset
        from transformers import (
            AutoModelForCausalLM,
            AutoTokenizer,
            BitsAndBytesConfig,
            DataCollatorForSeq2Seq,
            Trainer,
            TrainingArguments,
        )
        from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training
    except ImportError as e:
        print()
        print("ERROR: training dependencies are not installed.")
        print(f"  ({e})")
        print("Install them with:")
        print("    ./venv/Scripts/python.exe -m pip install -r requirements-train.txt")
        print("See TRAINING.md for the full setup + feasibility notes (bitsandbytes")
        print("on native Windows is finicky; WSL2 is the smoother path).")
        sys.exit(1)

    torch.manual_seed(SEED)

    has_cuda = torch.cuda.is_available()
    print(f"CUDA available: {has_cuda}")
    if has_cuda:
        print(f"  device: {torch.cuda.get_device_name(0)}")
        total_vram = torch.cuda.get_device_properties(0).total_memory / (1024 ** 3)
        print(f"  total VRAM: {total_vram:.1f} GB")
    else:
        print("ERROR: no CUDA GPU is visible. Trilobite will not silently fall back")
        print("to CPU weight training. Verify the CUDA torch build and driver, or")
        print("use `training plan` on a supported host.")
        return 3

    compute_dtype = torch.bfloat16 if (has_cuda and torch.cuda.is_bf16_supported()) else torch.float16

    print(f"Loading tokenizer + base model ({BASE}) in 4-bit NF4 ...")
    tokenizer = AutoTokenizer.from_pretrained(BASE, trust_remote_code=True)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    allow_offload = os.environ.get("TRILOBITE_ALLOW_CPU_OFFLOAD", "0") == "1"
    bnb_config = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_compute_dtype=compute_dtype,
        bnb_4bit_use_double_quant=True,
        llm_int8_enable_fp32_cpu_offload=allow_offload,
    )

    model_kwargs = {
        "quantization_config": bnb_config,
        "device_map": "auto" if allow_offload else {"": 0},
        "trust_remote_code": True,
    }
    if allow_offload:
        gpu_budget = os.environ.get("TRILOBITE_TRAIN_GPU_BUDGET_GB", "").strip()
        ram_budget = os.environ.get("TRILOBITE_TRAIN_RAM_BUDGET_GB", "").strip()
        if gpu_budget and ram_budget:
            model_kwargs["max_memory"] = {
                0: f"{max(1, int(float(gpu_budget)))}GiB",
                "cpu": f"{max(2, int(float(ram_budget)))}GiB",
            }
    model = AutoModelForCausalLM.from_pretrained(BASE, **model_kwargs)
    model.gradient_checkpointing_enable()
    model = prepare_model_for_kbit_training(model)

    lora_config = LoraConfig(
        r=LORA_R,
        lora_alpha=LORA_ALPHA,
        lora_dropout=LORA_DROPOUT,
        target_modules=LORA_TARGET_MODULES,
        bias="none",
        task_type="CAUSAL_LM",
    )
    model = get_peft_model(model, lora_config)
    model.print_trainable_parameters()

    print("Formatting + tokenizing examples (prompt masked, assistant-only loss) ...")
    formatted = [
        build_supervised_example(tokenizer, ex["messages"], MAX_LEN)
        for ex in examples
    ]
    dataset = Dataset.from_list(formatted)

    data_collator = DataCollatorForSeq2Seq(
        tokenizer=tokenizer,
        padding=True,
        label_pad_token_id=-100,
    )

    training_args = TrainingArguments(
        output_dir=os.path.join(OUTPUT_DIR, "checkpoints"),
        per_device_train_batch_size=PER_DEVICE_BATCH_SIZE,
        gradient_accumulation_steps=GRAD_ACCUM_STEPS,
        num_train_epochs=NUM_EPOCHS,
        learning_rate=LEARNING_RATE,
        lr_scheduler_type=LR_SCHEDULER,
        warmup_ratio=WARMUP_RATIO,
        logging_steps=LOGGING_STEPS,
        save_strategy=SAVE_STRATEGY,
        bf16=(compute_dtype == torch.bfloat16),
        fp16=(compute_dtype == torch.float16),
        gradient_checkpointing=True,
        report_to=[],
        seed=SEED,
    )

    trainer = Trainer(
        model=model,
        args=training_args,
        train_dataset=dataset,
        data_collator=data_collator,
    )

    print("Starting training ... (this is the attended part — watch VRAM)")
    checkpoints_dir = os.path.join(OUTPUT_DIR, "checkpoints")
    resume = None
    if os.environ.get("TRILOBITE_RESUME", "1") == "1" and os.path.isdir(checkpoints_dir):
        candidates = [
            os.path.join(checkpoints_dir, name)
            for name in os.listdir(checkpoints_dir)
            if name.startswith("checkpoint-") and name.split("-")[-1].isdigit()
        ]
        if candidates:
            resume = max(candidates, key=lambda path: int(path.rsplit("-", 1)[-1]))
            print(f"Resuming from checkpoint: {resume}")
    try:
        trainer.train(resume_from_checkpoint=resume)
    except (torch.cuda.OutOfMemoryError, RuntimeError) as exc:
        if not isinstance(exc, torch.cuda.OutOfMemoryError) and "out of memory" not in str(exc).lower():
            raise
        print("ERROR: CUDA out of memory. Training stopped cleanly; existing")
        print("checkpoints are preserved. Re-run `training plan` with a smaller")
        print("model/sequence length or enable supported CPU offload explicitly.")
        if has_cuda:
            torch.cuda.empty_cache()
        return 4

    print(f"Saving LoRA adapter to {OUTPUT_DIR} ...")
    model.save_pretrained(OUTPUT_DIR)
    tokenizer.save_pretrained(OUTPUT_DIR)
    manifest_path = os.environ.get("TRILOBITE_TRAINING_MANIFEST", "")
    manifest = {}
    if manifest_path and os.path.exists(manifest_path):
        with open(manifest_path, "r", encoding="utf-8") as stream:
            manifest = json.load(stream)
    manifest.update({
        "base_hf": BASE,
        "completed_ts": int(time.time()),
        "adapter_config": os.path.join(OUTPUT_DIR, "adapter_config.json"),
    })
    with open(os.path.join(OUTPUT_DIR, "training-manifest.json"), "w", encoding="utf-8") as stream:
        json.dump(manifest, stream, indent=2, sort_keys=True)
        stream.write("\n")
    print("Done. Run `training deploy` for checked GGUF conversion and validation.")
    return 0


def _entrypoint():
    try:
        return main()
    except RuntimeError as exc:
        if "out of memory" not in str(exc).lower():
            raise
        print("ERROR: CUDA out of memory while loading/preparing the model.")
        print("No CPU fallback was attempted. Preserve any existing checkpoints and")
        print("re-run `training plan` with a smaller model or sequence length.")
        return 4


if __name__ == "__main__":
    raise SystemExit(_entrypoint())
