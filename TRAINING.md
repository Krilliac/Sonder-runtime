# Training trilobite — QLoRA fine-tuning guide

> ⚠️ **Needs an attended first run. Do NOT run `qlora_train.py` unattended**
> (not from a fleet, `/loop`, cron, or CI). Watch VRAM (`nvidia-smi` / Task
> Manager) for the first few minutes and be ready to Ctrl-C on OOM. This
> harness has been prepared and validated to *parse*, but has never been
> executed — treat the first run as a debugging session, not a sure thing.

## What this does

trilobite's MCP server (`memory_store.py` + `reward.py`) records interactions
and their outcomes (`tests_passed`, `accepted`, `compiled`, ...). This is
sub-project #3 of that loop: instead of only retrieving distilled "lessons"
at inference time, actually **fine-tune weights** on the good-outcome
examples.

Pipeline:
1. `export_training_data.py` — pulls good-outcome (task, response) pairs from
   `memory.db` into `training_data.jsonl` (chat format: `{"messages": [user,
   assistant]}`), deduped by task text.
2. `qlora_train.py` — loads a Qwen2.5-Coder base in 4-bit (NF4) via
   `bitsandbytes`, attaches a LoRA adapter (`peft`) over the attention + MLP
   projections, and trains on the JSONL with the Qwen chat template,
   masking the prompt so loss only counts the assistant span. Saves the
   adapter to `./trilobite-lora/`.
3. Register the resulting adapter with Ollama so `trilobite`/`qwen2.5-coder`
   inference actually uses it (see "Registering with Ollama" below).

## Feasibility reality — read this before running anything

**Dataset size.** `training_data.jsonl` currently has **~60 examples**. That
is enough to prove the pipeline runs end-to-end (tokenization, masking,
LoRA attaches, a training loop executes, an adapter saves) — it is **not**
enough data to expect a meaningful behavior change in the model. Treat a
run today as a proof-of-pipeline, not a capability upgrade. To get real
gains: keep using trilobite day to day, keep calling `record_outcome(...)`
on real work, and re-run `export_training_data.py` periodically to grow the
dataset (hundreds of examples, not tens, is a more realistic bar before a
fine-tune is likely to move the needle).

**7B on a 6 GB RTX 4050 is marginal.** The box here is a 6 GB RTX 4050 +
16 GB system RAM on Windows. A 7B model's weights alone are ~4-4.5 GB in
4-bit NF4; add LoRA optimizer state, activations, and the OS/desktop's own
VRAM usage, and 6 GB commonly is not enough — expect OOM. Realistic paths,
in order of how much friction they save:

- **(a) Train a smaller base locally (recommended default).** `qlora_train.py`
  defaults `BASE` to `Qwen/Qwen2.5-Coder-1.5B-Instruct`, which fits
  comfortably in 6 GB with headroom for optimizer state and activations.
  This is the path this harness is tuned for out of the box. A 3B variant
  is a plausible middle ground if 1.5B trains cleanly and you want to push
  further, still locally.
- **(b) Train the 7B via WSL2/Linux.** `bitsandbytes` on native Windows is
  known to be flaky (prebuilt wheels lag CUDA/Python combos; CPU-offload
  code paths are much better exercised on Linux). Running the identical
  script inside WSL2 — same GPU, same 6 GB ceiling, but a more reliable
  bitsandbytes — plus `device_map="auto"` with
  `llm_int8_enable_fp32_cpu_offload=True` to spill some layers to system RAM
  is the more realistic way to attempt 7B on this hardware.
- **(c) Train the 7B on a cloud GPU.** Rent a 16-24 GB+ GPU (e.g. a cloud
  A10/L4/3090-class instance) for the duration of the run if you specifically
  need the 7B fine-tuned and don't want to fight VRAM. Overkill for today's
  ~60-example dataset, but the right call once the dataset is large and 1.5B
  results look promising.

Bottom line: **run this tomorrow morning against the 1.5B default first.**
It's the only path in this list that's actually likely to complete on this
machine without extra setup.

## Exact steps

```bash
cd "/c/Users/user/.claude/mcp-servers/local-llm"

# 1. Install training deps (NOT installed by default — heavier than the
#    normal test/runtime requirements).
./venv/Scripts/python.exe -m pip install -r requirements-train.txt

# 2. (Re)export the latest good-outcome examples from memory.db.
./venv/Scripts/python.exe export_training_data.py

# 3. Train. Attended — watch VRAM. Uses the 1.5B base by default.
./venv/Scripts/python.exe qlora_train.py

# Optional: attempt the 7B base instead (see feasibility notes above —
# likely needs WSL2 or CPU offload to avoid OOM on 6 GB).
TRILOBITE_BASE=Qwen/Qwen2.5-Coder-7B-Instruct ./venv/Scripts/python.exe qlora_train.py
```

Training writes the LoRA adapter to `./trilobite-lora/` (adapter weights +
tokenizer files; gitignored — it's a build artifact, not source).

## Registering the adapter with Ollama

Ollama's `Modelfile` supports an `ADAPTER` directive pointing at a LoRA
adapter, but **format compatibility is the catch**: Ollama (via llama.cpp)
expects the adapter in **GGUF** form, not the raw Hugging Face/PEFT
`adapter_model.safetensors` that `qlora_train.py` produces directly. Concept:

1. Convert the PEFT adapter to GGUF using llama.cpp's conversion script
   (typically `convert_lora_to_gguf.py` in the llama.cpp repo — check the
   current llama.cpp docs/repo for the exact script name and flags, they
   move around between releases).
2. Write a Modelfile referencing the *matching* base and the converted
   adapter, e.g.:
   ```
   FROM qwen2.5-coder:1.5b
   ADAPTER ./trilobite-lora/trilobite-lora.gguf
   ```
   (Use `qwen2.5-coder:1.5b` if you trained the 1.5B default; match whatever
   `BASE` you actually trained against, or the adapter's weight shapes won't
   line up.)
3. `ollama create trilobite-tuned -f Modelfile` and try it with `ollama run
   trilobite-tuned`.

This conversion step is honestly the least-tested part of this whole guide —
llama.cpp's LoRA/GGUF tooling has changed shape across versions. Budget time
to fight it, and don't be surprised if the adapter format that
`transformers`/`peft` produced needs an extra conversion flag or a newer
llama.cpp checkout than whatever you have installed. Verifying this
end-to-end is exactly the kind of thing that needs attended, interactive
debugging — hence the banner at the top of this doc.

## Files

- `export_training_data.py` — memory.db → training_data.jsonl (already exists, unchanged).
- `qlora_train.py` — the training script (this deliverable). Heavy imports
  (`torch`, `transformers`, `peft`, `bitsandbytes`) are deferred inside
  `main()`, so the file parses/`py_compile`s fine even without those
  packages installed — it just prints an instructive error and exits if you
  run it without `pip install -r requirements-train.txt` first.
- `requirements-train.txt` — training-only deps, kept separate from
  `requirements-dev.txt` so the normal test suite stays fast/light.
- `trilobite-lora/` — training output (adapter + tokenizer files), created
  by a successful run. Not present yet; gitignored once it exists.
