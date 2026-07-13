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
/training adopt-legacy --confirm
/training release-alias --confirm
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
python adaptive_training.py adopt-legacy --confirm
python adaptive_training.py release-alias --confirm
python adaptive_training.py rollback
```

Planning options include `--model auto|1.5b|3b|7b`,
`--allow-cpu-offload`, `--max-vram`, `--max-system-ram`,
`--context-length`, `--sequence-length`, `--batch-size`, `--gpu-index`, and
`--gradient-accumulation`. `--full-finetune` is a feasibility/planning switch
only; the attended start path intentionally supports QLoRA and rejects dense
training. Corresponding `SONDER_*` environment overrides remain supported; see
`adaptive_training.py` for the exact names.

Every confirmed start creates a unique run directory under
`sonder-personal-lora/runs/<run-id>/`. Its plan records the exact base, adapter
path, selected physical GPU, and SHA-256 of the approved dataset. The controller
passes a fresh five-minute, one-use launch capability to `qlora_train.py`; direct
script invocation and replay are rejected before heavyweight ML imports. Set
`--gpu-index` to bind the physical CUDA device before Torch initializes.

For each new run, the default dataset is freshly exported from shared memory
directly into that run directory; an old repo-level `training_data.jsonl` is not
silently reused. The exporter requires grounded positive outcomes, vetoes an
interaction if it has any negative or unknown outcome, excludes task/response
text caught by the shared path/credential/privacy rules, and deterministically
chooses the strongest then newest response for repeated normalized prompts. It
writes atomically and records only aggregate rejection counts plus the dataset
SHA-256 in `training-data.jsonl.manifest.json`. Set `SONDER_DATA` only when you
intentionally want a separately curated JSONL input. Resume never re-exports:
it verifies and reuses the exact immutable snapshot authorized for that run.

The training child treats the authorized JSONL as all-or-nothing. Invalid UTF-8
or JSON, blank/oversized rows, unsupported message structure, empty content, or
dataset bounds violations abort the run instead of silently dropping examples.

`--allow-cpu-offload` is retained as an explicit capability request, but the
current bitsandbytes/Trainer backend rejects it. Hugging Face documents the
available `device_map="auto"` mechanism as an inference-only path, so Sonder
Runtime does not present it as safe QLoRA training or silently attempt it.
Select a smaller GPU-resident plan instead. CPU offload can be enabled in a
future backend only after that backend has a supported implementation and
attended validation coverage.

Training dependencies are intentionally separate:

```bash
# Install the CUDA wheel that matches the host first (the validated Windows
# host currently uses the PyTorch cu130 index), then install the bounded stack.
python -m pip install torch --index-url https://download.pytorch.org/whl/cu130
python -m pip install -r requirements-train.txt
python -c "import torch; assert torch.cuda.is_available(); print(torch.__version__, torch.version.cuda)"
```

On native Windows, WSL2 remains the more reliable bitsandbytes environment.
Sonder Runtime refuses silent CPU training and stops CUDA OOM failures with
checkpoints intact. Resume is never inferred from a shared output folder: use
`start --confirm --resume` to reauthorize the recorded interrupted/failed run.
The base model, sequence length, batch size, gradient accumulation, and GPU must
match that run's manifest before its checkpoints can be resumed.

Each default run exports directly into a run-local immutable snapshot; an
explicit `SONDER_DATA` file is copied into that same snapshot boundary. The
trainer hashes the same byte stream it parses and never rereads mutable source
data for that run. The supported Qwen Hugging Face repositories are
pinned to reviewed 40-character commits, recorded in the plan/manifest, loaded
with `trust_remote_code=False`, and must match on resume.

## Qwen adapter deployment

Ollama documents direct Safetensors adapters only for selected architectures;
Qwen is not in that list. Sonder Runtime therefore never assumes raw PEFT
Safetensors will load. Deployment requires llama.cpp commit
`99f3dc32296f825fec94f202da1e9fede1e78cf9` and runs
`convert_lora_to_gguf.py` against a local `config.json` fetched from the
exact reviewed 40-character Hugging Face commit recorded by PEFT and Sonder
Runtime's training manifest. The staged config is hashed into the deployment
receipt and passed with llama.cpp's local `--base` option, so conversion never
resolves a mutable Hub HEAD. PEFT and Hugging Face perform adapter training
first; llama.cpp converts the result to GGUF; Ollama then stores and serves the
validated deployment artifact. Before execution, Sonder reads the exact commit
objects with Git replacement objects disabled, compares their full tree listing
to a hard-coded reviewed seal, archives only `convert_lora_to_gguf.py`,
`conversion/`, and `gguf-py/` into isolated staging, and records both the Git
tree and extracted-file manifest hashes in the receipt. Conversion runs from
that immutable snapshot under Python isolated mode, so live-worktree shadow
modules, ignored files, Git replace refs, user site packages, and checkout
TOCTOU changes cannot enter the converter process.

`sonder-personal:latest` is an endpoint-global alias, so only one runtime-policy
file may own it. New deployments create that owner record automatically after
the alias, policy, evaluation receipt, and training state all commit. An alias
created by an older Sonder version has no owner record and is never adopted or
overwritten implicitly. Inspect the active policy and exact local alias first,
then explicitly bind it with `/training adopt-legacy --confirm`; this records
its normalized identity and Ollama manifest digest without copying, deleting, or
rerouting the model. A different policy cannot adopt an already-owned alias. To
move ownership deliberately, first `/training rollback` code/general and use
the runtime-policy command to move any legacy `fast` reference off the personal
alias, then run `/training release-alias --confirm`; release removes
only the owner record, never the model or routing, after proving the current
policy no longer references it. The other policy must still adopt explicitly.

Deployment then:

1. Requires the exact completed run recorded by the lifecycle controller;
   verifies its plan, base identity, adapter path, completion capability, and
   controller-held SHA-256/size records for config and weight artifacts.
2. Copies the hash-verified config and weights into an isolated deployment
   staging directory, verifies the staged bytes again, and converts only that
   immutable snapshot to an F16 GGUF adapter.
3. Creates a uniquely named candidate in Ollama using the exactly mapped
   `qwen2.5-coder:1.5b`, `:3b`, or `:7b` base.
4. Pins the exact local Ollama manifest digests, then evaluates base and
   candidate sequentially on four held-out SQLite task families whose table
   identifiers vary from a per-deployment nonce. Model SQL runs only as a
   single read-only query in resource-bounded in-memory databases with hidden
   adversarial fixtures; arbitrary generated code is never executed. A dynamic
   nonce/case/reversal/arithmetic JSON probe separately checks precise
   instruction following. Promotion requires at least 3/4 SQL tasks, the
   instruction probe, no per-task regression, and measurable lift when the base
   is imperfect. This is a conservative deployment safety canary, not a claim
   that this bounded suite measures every general capability.
5. Writes a bounded, hashed evaluation receipt containing scores, reason codes,
   and artifact hashes—not prompts, generated SQL, or hidden fixture data.
6. Distinguishes a proven-absent personal alias from a transient `ollama show`
   failure. Any existing alias is copied and verified before promotion.
7. Promotes the candidate with `ollama cp`, requires the published alias digest
   and normalized model identity to equal the candidate, then reruns the
   complete executable SQL suite through `sonder-personal:latest` and rejects
   any candidate-pass to published-alias-fail regression.
8. Gives each candidate durable cleanup-ledger ownership before `ollama create`,
   including partial-create failures. After the candidate gate passes, a marker
   stored beside the canonical runtime-policy file atomically captures the
   policy revision and blocks ordinary writers before any backup or publication
   alias mutation. Processes sharing a policy cannot bypass that marker by
   inheriting different `SONDER_HOME` values.
9. Serializes the endpoint-global `sonder-personal:latest` namespace even when
   processes use different homes, state files, and runtime policies. A durable
   per-Ollama transition marker survives process death, while a persistent owner
   record prevents a second policy from silently replacing an alias still routed
   by the first policy. Ownership is committed only with the verified model
   identity/digest and training state.
10. Uses revision-checked policy updates and a shared training/deploy/rollback
   lifecycle lock. Alias, policy, and state commit failures restore the verified
   previous alias and prior policy without overwriting a concurrent policy edit.
11. Advances the recovery marker before each reversible phase. If the process
    stops between candidate creation, alias backup/publication, policy
    activation, and state commit, the next deploy or rollback restores the
    verified alias/policy before doing new work. Failed alias removals remain in
    the marker or enter a bounded, policy-adjacent cleanup ledger; they are never
    forgotten on a failed `ollama rm`. Stale run-local staging is swept under the
    lifecycle lock. The new routing remains active only after its receipt and
    state commit succeed.

`sonder:latest` is never overwritten or deleted. Existing 1.5B/3B/7B
base models, trusted source adapters, checkpoints, and the latest verified
recovery alias are also preserved. Temporary GGUF staging files and rejected
candidate aliases are cleaned automatically.

`--adapter-dir` cannot import arbitrary external adapters: when supplied, it
must resolve to the exact adapter directory bound to the current trusted
training state. External import needs a separate audited workflow rather than
rewritable metadata in an untrusted directory.

## Rollback

Run:

```text
/training rollback
```

or:

```bash
python adaptive_training.py rollback
```

This first verifies `sonder:latest`, then atomically reserves the runtime policy
and keeps that transition marker through the revision-checked policy update and
training-state commit. A process stop restores the prior policy unless the state
contains the matching commit witness; ordinary policy writers cannot interleave
between those two commits. Rollback intentionally leaves the personal model and
all training artifacts in place for diagnosis or redeployment.
