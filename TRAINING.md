# Adaptive weight training in Sonder Runtime

> **Contract scope:** this focused contract describes current behavior. Unfinished
> implementation work is tracked in the
> [master implementation specification](docs/architecture/SONDER-MASTER-IMPLEMENTATION-SPEC.md); the
> [behavior status](#behavior-status) table labels what is implemented,
> experimental, proposed, degraded, or unsupported.

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
python adaptive_training.py start --confirm --model auto
python adaptive_training.py start --confirm --resume --model auto
python adaptive_training.py status
python adaptive_training.py deploy --llama-cpp /path/to/llama.cpp
python adaptive_training.py adopt-legacy --confirm
python adaptive_training.py release-alias --confirm
python adaptive_training.py rollback
```

Run the lifecycle from the training environment described in
[Set up a training host](#set-up-a-training-host): `start` launches the trainer
with the Python interpreter that runs the command. `start` always exports a
fresh dataset into the new run directory itself, so running the standalone
`python export_training_data.py` first is optional and only useful to inspect
what would be exported (it writes `training_data.jsonl` in the current
directory); `start` does not read that file.

Planning options include `--model auto|1.5b|3b|7b` or any other explicit
parameter size such as `14b` or `27b`. A size outside the pinned 1.5B/3B/7B
catalog gets an *inference-only* recommendation from a parameter-count heuristic (4-bit
weights, a per-parameter KV slope and fixed runtime overhead); that is an
estimate, not proof that the model fits, and serve-time residency may still
need a smaller context or model. Training never uses an uncatalogued base and
never substitutes a catalog base for one: an explicit uncatalogued size
disables the QLoRA and dense recommendations, the plan records a rejection note
naming the requested size, and `start --model 27b --confirm` refuses to launch.
Use `--model auto` (or `1.5b`, `3b`, `7b`) to train. Other planning
options are `--allow-cpu-offload`, `--max-vram`, `--max-system-ram`,
`--context-length`, `--sequence-length`, `--batch-size`, `--gpu-index`, and
`--gradient-accumulation`. `--full-finetune` is a feasibility/planning switch
only; the attended start path intentionally supports QLoRA and rejects dense
training. Corresponding `SONDER_*` environment overrides remain supported; the
planning and training variables are in the
[Environment reference](#environment-reference), which also lists the
hardware-detection overrides that change `plan` results.

`[capacity].training_jobs` (default `1`) is validated but has no effect on
training. It must be at least `1`: `0` is a configuration error
(`[capacity].training_jobs must be >= 1`) that stops Sonder from loading its
config. At any valid value (1, 2, 8) it changes nothing: setting it to `2` does
not allow two concurrent runs. Only one run at a time is possible regardless of
the key, because runs are serialized by non-blocking lifecycle file locks (a
second start while one is active is refused). There is no `SONDER_*` variable
for the key. The `[features].training` flag does not gate training either: it is
exported as `SONDER_TRAINING` but nothing in the training code reads it.

Every confirmed start creates a unique run directory under
`<run root>/runs/<run-id>/` (see [Outputs and state](#outputs-and-state) for the
run root). Its plan records the exact base, adapter
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
Select a smaller GPU-resident plan instead. CPU-offloaded training is
unsupported; the planner reports the offload request as rejected rather than
approximating it.

## Set up a training host

Checked against the code on 2026-11-20. Training is optional: nothing else in
Sonder needs these packages, and the normal install
([install-workstation-local](docs/runbooks/install-workstation-local.md)) does
not provide them. Complete this section once per machine that will run
`start`.

### 1. Hardware

- **GPU:** an NVIDIA GPU with a working CUDA runtime and driver. Other GPU
  vendors and CPU-only hosts get `Local QLoRA disabled: this bitsandbytes path
  requires a supported NVIDIA CUDA runtime.` in the plan. Sonder reads the GPU
  through `nvidia-smi`, so `nvidia-smi` must work in the shell you will use.
- **Memory:** the planner keeps GPU and system memory as separate budgets and
  reads *live free* values, so a busy desktop lowers what is offered.
  - Usable RAM = available RAM minus the larger of 2 GB or 25% of total RAM.
  - Usable VRAM = free VRAM minus a reserve of 2.0 GB when the card has 12 GB or
    more in total, otherwise 1.0 GB.
  - `--model auto` picks the largest of 7B, 3B, 1.5B that passes the table
    below, and `--model 7b` (etc.) tests only that size.

| Base | Free VRAM needed to be considered | Catalog estimate at sequence length 1024, batch 1 (VRAM / RAM) | Inference estimate (VRAM / RAM) |
|---|---|---|---|
| 1.5B | 4.0 GB | 2.8 GB / 6.0 GB | 1.6 GB / 3.0 GB |
| 3B | 7.5 GB | 5.0 GB / 10.0 GB | 2.8 GB / 5.0 GB |
| 7B | 11.5 GB, and usable RAM of at least 16 GB | 10.0 GB / 18.0 GB | 5.5 GB / 9.0 GB |

  A size is chosen only if the estimate also fits inside the usable VRAM and
  usable RAM budgets above, so in practice a 7B run wants a 16 GB class card
  with the desktop mostly idle. A longer `--sequence-length` or larger
  `--batch-size` raises the estimates. Both the estimates and the thresholds
  are conservative planning numbers, not measurements; the live check is
  `plan` (step 5).
- **System RAM is the usual blocker.** A host with plenty of VRAM but little
  *available* RAM gets `Training: disabled` with a rejected-alternatives line
  such as `~6.0 GB RAM exceeds 0.3 GB budget`. Close other applications and
  re-run `plan`.
- **Disk** (checked before each step):
  - A run needs `3 + 2.2 x billions of parameters` GB free on the drive that
    holds the run root: about 6.3 GB (1.5B), 9.6 GB (3B), 18.4 GB (7B).
  - `deploy` needs `1.5 x billions of parameters + 2` GB free on the drive that
    holds the run: about 4.3 GB, 6.5 GB, 12.5 GB.
  - The Hugging Face download cache and the Ollama model store are
    additional and are not sized by Sonder.
- The trainer uses bf16 when the GPU supports it and fp16 otherwise.

### 2. Training environment (separate virtual environment)

The training stack is deliberately not part of `requirements-runtime.txt`. Use
a separate virtual environment so the production runtime stays light.
`start` runs the trainer with the Python interpreter that executes the `start`
command, so **run `start` (and `/training start`) from the training
environment**. If the process running `start` is the normal runtime
interpreter, the trainer exits with code 1 and prints
`ERROR: training dependencies are not installed.` plus the install command.
Python 3.11 or newer is recommended (`requirements-train.txt` notes that the
PEFT 0.22.x line of its `peft>=0.19,<0.23` pin needs it).

PowerShell, from the repository checkout:

```powershell
python -m venv venv-train
.\venv-train\Scripts\python.exe -m pip install --upgrade pip
.\venv-train\Scripts\python.exe -m pip install -r requirements-runtime.txt
# CUDA build of PyTorch first; cu130 is the index the validated Windows host uses.
.\venv-train\Scripts\python.exe -m pip install torch --index-url https://download.pytorch.org/whl/cu130
.\venv-train\Scripts\python.exe -m pip install -r requirements-train.txt
.\venv-train\Scripts\python.exe -c "import torch; assert torch.cuda.is_available(); print(torch.__version__, torch.version.cuda)"
```

Linux or WSL2 (bash):

```bash
python3 -m venv venv-train
./venv-train/bin/python -m pip install --upgrade pip
./venv-train/bin/python -m pip install -r requirements-runtime.txt
./venv-train/bin/python -m pip install torch --index-url https://download.pytorch.org/whl/cu130
./venv-train/bin/python -m pip install -r requirements-train.txt
./venv-train/bin/python -c "import torch; assert torch.cuda.is_available(); print(torch.__version__, torch.version.cuda)"
```

Expected: the last command prints a `2.13.x` PyTorch version and the CUDA
version of the wheel. An `AssertionError` means a CPU-only torch or a driver
problem; reinstall torch from the CUDA index. Pins (from
`requirements-train.txt`): `torch>=2.13,<2.14`, `transformers>=5.3,<6`,
`accelerate>=1.1,<2`, `peft>=0.19,<0.23`, `datasets>=2.19,<2.21`,
`bitsandbytes>=0.49,<0.50`. Install torch from the CUDA index *before* the
requirements file, otherwise pip may pick the plain PyPI wheel. Change the
index suffix (`cu130`) only if the driver is older; check `nvidia-smi` first.

`requirements-runtime.txt` is installed too so the lifecycle's own imports
resolve in this interpreter. The lifecycle commands `hardware`, `plan` and
`status` are verified to run under the normal runtime environment (they do not
import the ML stack); only `start` needs the training environment. Whether the
runtime pins are strictly required in the training environment is not
verified; installing them is the conservative choice.

### 3. Hugging Face downloads

The base model is downloaded on the first run by `transformers` from the
Hugging Face Hub, so the host needs outbound HTTPS to the Hub and room in the
Hugging Face cache. Sonder pins each supported base to one reviewed commit and
loads it with `trust_remote_code=False`:

| Base | Repository | Pinned commit |
|---|---|---|
| 1.5B | `Qwen/Qwen2.5-Coder-1.5B-Instruct` | `2e1fd397ee46e1388853d2af2c993145b0f1098a` |
| 3B | `Qwen/Qwen2.5-Coder-3B-Instruct` | `488639f1ff808d1d3d0ba301aef8c11461451ec5` |
| 7B | `Qwen/Qwen2.5-Coder-7B-Instruct` | `c03e6d358207e414f1eca0bb1891e29f1db0e242` |

Sonder reads no Hugging Face variables itself. The standard Hugging Face
variables apply because the Hub client reads them: `HF_HOME` moves the cache to
a drive with room, and `HF_TOKEN` supplies a token if your network or the
repository requires one (the pinned Qwen repositories are public models; whether
your account needs a token is not something Sonder checks). `deploy` downloads
only `config.json` for the same pinned commit (see
[Qwen adapter deployment](#qwen-adapter-deployment)), so it needs Hub access
too.

### 4. Ollama and the llama.cpp converter (needed only to deploy)

Training itself needs neither. To `deploy` a trained adapter the same machine
needs:

1. **Ollama CLI and server on loopback**, with the matching base model pulled
   (the exact tag is checked; no substitute is used):

   ```powershell
   ollama pull qwen2.5-coder:1.5b   # or :3b / :7b, matching the trained base
   ollama list
   ```

   Expected: the tag appears in `ollama list`. Sonder runs `ollama` from
   `SONDER_OLLAMA_EXE` if set, else from `PATH`.
   The deploy target alias is `sonder-personal:latest`; the stable alias
   `sonder:latest` is never overwritten.
2. **`git` on `PATH`.** Sonder reads the converter from Git objects.
3. **A llama.cpp checkout that contains commit
   `99f3dc32296f825fec94f202da1e9fede1e78cf9`**, with `convert_lora_to_gguf.py`
   at its root:

   ```bash
   git clone https://github.com/ggml-org/llama.cpp.git <llama-cpp-dir>
   git -C <llama-cpp-dir> checkout 99f3dc32296f825fec94f202da1e9fede1e78cf9
   ```

   Clone it outside the Sonder checkout (for example next to it) and point
   `SONDER_LLAMA_CPP` or `--llama-cpp` at it. Cloning to `<repo>/llama.cpp`
   works for the search below, but that path is not gitignored, so it leaves an
   untracked nested repository in your checkout.

   The deploy step requires the checkout to be a full Git repository that
   *contains* that commit (a shallow clone that lacks it fails with
   `llama.cpp checkout does not contain reviewed converter commit ...`), and
   then reads the converter from that commit's Git objects, not from the
   working tree. The checkout command above keeps the working tree consistent
   with what is used. Sonder looks for the checkout, in order, at the
   `--llama-cpp` argument, `SONDER_LLAMA_CPP`, `<repo>/llama.cpp`, then
   `<repo>/third_party/llama.cpp`. The path may be the checkout directory or the
   `convert_lora_to_gguf.py` file itself. If none is found, deploy stops with
   `Deployment blocked: llama.cpp/convert_lora_to_gguf.py was not found.`
   The clone URL above is the upstream llama.cpp project; the repository does
   not pin or verify the URL, only the commit.
4. **Converter Python packages.** The converter runs under the same
   interpreter that runs `deploy`, in isolated mode with user site-packages
   disabled, so everything it imports must be installed in that environment.
   Sonder itself imports `huggingface_hub` to fetch the pinned `config.json`.
   The exact import list of the pinned llama.cpp converter was not verified
   here (no checkout is shipped in this repository): if conversion fails with
   an `ImportError`, install the package it names into the same environment, and
   consult the converter's own requirements in the checkout.

### 5. Verify the setup

Run these from the training environment. Nothing starts training.

```powershell
.\venv-train\Scripts\python.exe adaptive_training.py hardware
.\venv-train\Scripts\python.exe adaptive_training.py plan --dry-run --model auto
```

(or `/hardware` and `/training plan --dry-run` in the REPL). Expected:
`GPU: nvidia <your card> | runtime: CUDA` in the first command, and in the
second a `Training: QLoRA (4-bit NF4) <size>, GPU 0, batch 1, gradient
accumulation 8` line followed by the chosen base. `Training: disabled` plus a
`Rejected alternatives` list names the budget that failed (see the hardware
notes above). Then start the first run attended:

```powershell
.\venv-train\Scripts\python.exe adaptive_training.py start --confirm --model auto
```

Expected: a `Loaded N training examples` line, `CUDA available: True`, then
Trainer progress, and finally `Training completed; adapter saved at ...`. A
run that dies partway leaves checkpoints; see [Runs, resume and dataset
rules](#runs-resume-and-dataset-rules). Deploying is covered by
[Qwen adapter deployment](#qwen-adapter-deployment); the training-failure
procedure is
[docs/runbooks/training-failure.md](docs/runbooks/training-failure.md).

### Remote or Caddy-fronted Ollama

Deployment, promotion, `adopt-legacy`, `release-alias`, `rollback` and
`start --confirm` all serialize on a per-user lock keyed by the *loopback*
Ollama endpoint, and the promotion canary talks to that endpoint directly.
They refuse a configured Ollama host that is not loopback; the remote-Ollama
opt-in (`SONDER_ALLOW_REMOTE_OLLAMA`) does **not** lift this. With a non-loopback
host configured, `start --confirm` stops with `Training failed safely:
promotion evaluation requires a valid loopback Ollama host: ...`, and the other
lifecycle commands fail on the same check.

What this means for a setup where chat reaches a remote Ollama (for example one
behind a Caddy reverse proxy, see
[secure-remote-access](docs/runbooks/secure-remote-access.md)):

- **Train anywhere that has the GPU and keeps a loopback (or unset)
  `OLLAMA_HOST`.** Training itself never contacts Ollama, so the training host
  does not need Ollama running. In the REPL a configured Ollama host from the
  Sonder configuration takes precedence over the environment, so for training
  commands use the standalone command in a shell where `OLLAMA_HOST` is unset
  or loopback (PowerShell: `Remove-Item Env:OLLAMA_HOST` for that shell).
- **Deploy, adopt, release and roll back on the machine whose own Ollama is
  loopback**, because they run `ollama create`/`cp` against that server and
  update the runtime policy of the Sonder state home on that machine. A remote
  Ollama reached through Caddy cannot be the deploy target.
- Moving an adapter from a trainer machine to a different deploy machine is
  not supported; see [Training on a cloud GPU](#training-on-a-cloud-gpu).

### WSL2

Native Windows bitsandbytes is supported but less reliable; WSL2 is the more
reliable environment. Treat WSL2 as its own host: it has its own `venv-train`
(Linux wheels, same commands as above), its own state home, and its own per-user
lock directory. If you also train from native Windows, do not point both at one
state home. By default WSL2 already uses its own (`~/.local/share/sonder`), so
nothing is needed; only if `SONDER_HOME` inside WSL2 points into `/mnt/c` (the
Windows side) set a distinct one (for example
`export SONDER_HOME=$HOME/.sonder-wsl`). `deploy` from WSL2 needs an Ollama
server that is loopback *inside WSL2* (install Ollama in WSL2); a Windows-side
Ollama is not the same endpoint. `nvidia-smi` must work inside WSL2 (it is how
Sonder detects the GPU).

### Multi-GPU

`--gpu-index N` (default 0, env `SONDER_TRAIN_GPU_INDEX`) selects the physical
GPU: the planner measures that GPU, the controller sets `CUDA_VISIBLE_DEVICES`
to it, and inside the trainer it appears as device 0. The index is recorded in
the run's plan and must match when resuming.

### Outputs and state

| What | Where |
|---|---|
| Run root | `SONDER_LORA_OUT`, default `<repo checkout>/sonder-personal-lora`. It is **not** under the state home. |
| One run | `<run root>/runs/<32-hex run id>/` |
| In a run | `training-plan.json`, `training-data.jsonl` (plus `training-data.jsonl.manifest.json` when the data was freshly exported from memory; an explicit `SONDER_DATA` run writes no `.manifest.json`), `adapter/` (with `checkpoints/`, which is kept after training, and `training-manifest.json`), `.launch-claimed` while the trainer runs, `deployment-staging/<id>/` during deploy, `deployment-evaluations/<id>.json` receipts |
| Training state | `training_state.json` in the state home (`SONDER_HOME`, or the platform default). `SONDER_TRAINING_STATE` overrides it only when the process has no state-home override of its own. |
| Lifecycle locks | `training-lifecycle.lock` in the state home, `<runtime_policy file>.lifecycle.lock`, `<training state file>.lifecycle.lock`, and per-user `~/.sonder/locks/ollama-<hash>.alias.lock` (with `.alias-owner.json` / `.alias-transition.json` records). Lock files have `*.owner.json` sidecars naming the holder. They are file locks, held only while the process lives, and have no expiry. |

`status` stored in `training_state.json` is one of `running`, `interrupted`,
`failed`, `failed_validation`, `trained`, `deployed` and, after a rollback,
`rolled_back`. `status` reports `never_started` when the file does not exist yet
and `invalid` when it is unreadable or not a JSON object. The deploy canary
statuses `evaluating`, `passed_candidate_gate`, `rejected_candidate`,
`evaluation_error`, `passed_final_suite` and `failed_final_suite` are recorded
in the `deployment-evaluations/<id>.json` receipt, not in `training_state.json`.
Read the state with `/training status` or `python adaptive_training.py status`.

## Environment reference

Checked 2026-11-20. Flags and variables for `plan` and `start`; a flag wins
over its variable.

| Variable | Flag | Default | Effect |
|---|---|---|---|
| `SONDER_TRAIN_MODEL` | `--model` | `auto` | `auto`, `1.5b`, `3b`, `7b` (or an uncatalogued size for an inference-only plan) |
| `SONDER_MAX_VRAM_GB` | `--max-vram` | unset | cap on usable VRAM, GB |
| `SONDER_MAX_SYSTEM_RAM_GB` | `--max-system-ram` | unset | cap on usable system RAM, GB |
| `SONDER_CONTEXT_SIZE` | `--context-length` | `8192` | inference context used in the plan (minimum 512) |
| `SONDER_MAX_LEN` | `--sequence-length` | `1024` | training sequence length (minimum 128) |
| `SONDER_BATCH_SIZE` | `--batch-size` | `1` | per-device batch |
| `SONDER_GRAD_ACCUM` | `--gradient-accumulation` | `8` | gradient accumulation steps |
| `SONDER_TRAIN_GPU_INDEX` | `--gpu-index` | `0` | physical GPU |
| `SONDER_ALLOW_CPU_OFFLOAD` (`1`) | `--allow-cpu-offload` | off | only requests offload; the plan rejects it, and the trainer is always launched with it forced to `0` |
| `SONDER_FULL_FINETUNE` (`1`) | `--full-finetune` | off | feasibility report only; never started |
| none | `--dry-run` | | `plan` and `start`; see [Commands](#commands) |
| none | `--confirm`, `--resume` | | `start` only; see [Commands](#commands) |
| `SONDER_LORA_OUT` | none | `<repo>/sonder-personal-lora` | run root |
| `SONDER_DATA` | none | unset | use this JSONL instead of a fresh export (copied into the run) |
| `SONDER_TRAINING_STATE` | none | `<state home>/training_state.json` | state file (see Outputs and state) |
| `SONDER_LLAMA_CPP` | `deploy --llama-cpp` | unset | llama.cpp checkout for `deploy` |
| `SONDER_OLLAMA_EXE` | none | `ollama` on `PATH` | Ollama CLI used by `deploy`, `rollback`, `adopt-legacy` |
| `SONDER_LORA_R` / `_ALPHA` / `_DROPOUT` | none | `16` / `32` / `0.05` | LoRA hyperparameters, read by the trainer |
| `SONDER_EPOCHS` / `SONDER_LR` | none | `3` / `2e-4` | epochs and learning rate, read by the trainer |
| `SONDER_TRAIN_RAM_BUDGET_GB` | none | set by the controller | RAM budget used to cap tokenised data; leave unset |
| `SONDER_MODEL_LISTS` | none | `<state home>/model_lists.json` if present | model-catalog overlay, see [the model catalog page](docs/wiki/18-model-catalog.md) |

Hardware-detection overrides (read by `plan` and `start` through the system
profile, `sonder_runtime/platform/system_profile.py`): `SONDER_RAM_GB` (total RAM, which also sets the reserve in the usable-RAM
formula, so it changes `plan` results), `SONDER_AVAILABLE_RAM_GB`,
`SONDER_GPU_VENDOR`, `SONDER_VRAM_GB`, `SONDER_FREE_VRAM_GB`,
`SONDER_CUDA_AVAILABLE`, `SONDER_ROCM_AVAILABLE`, `SONDER_GPU_NAME` and
`SONDER_COMPUTE_CAPABILITY` (plus the NPU overrides `SONDER_NPU_PROBE`,
`SONDER_NPU_DETECTED`, `SONDER_NPU_VENDOR` and `SONDER_NPU_NAME`). They
replace what Sonder detects, so a wrong value changes the plan; leave them
unset on a real training host.

Other flags: `deploy --adapter-dir PATH` (accepts only the adapter bound to
local trusted state), `adopt-legacy --confirm`, `release-alias --confirm`.

The controller also sets `SONDER_BASE`, `SONDER_HF_REVISION`, `SONDER_RESUME`,
`SONDER_TRAINING_MANIFEST`, `SONDER_TRAINING_LAUNCH_TOKEN`,
`SONDER_TRAINING_PLAN_SHA256`, `SONDER_TRAINING_DATA_SHA256`,
`SONDER_TRAINING_RECEIPT` and `SONDER_TRAIN_GPU_BUDGET_GB` (the plan's usable
VRAM, alongside `SONDER_TRAIN_RAM_BUDGET_GB`) for the child. Do not set them yourself: launching
`qlora_train.py` directly is unsupported and is rejected.

## Runs, resume and dataset rules

On native Windows, WSL2 remains the more reliable bitsandbytes environment.
Sonder Runtime refuses silent CPU training and stops CUDA OOM failures with
checkpoints intact.

**Resume.** Resume is never inferred from a shared output folder. Use
`start --confirm --resume` (with the same planning options) only when the state
status is `interrupted` or `failed`; it re-authorizes that recorded run. The
base model, base revision, sequence length, batch size, gradient accumulation
and GPU index must match the run's plan, the run directory must be inside the
configured run root, and the recorded dataset snapshot must still hash the same.
An explicit `SONDER_DATA` file is re-validated on resume. The trainer continues
from the newest `checkpoints/checkpoint-N`. Ctrl-C during `start` records
`interrupted`. Resume is refused while the earlier trainer is still alive (or
its identity cannot be proven). The controller does not refuse a new `start`
because of a stale `running` status; it refuses only while a recorded trainer is
alive.

**Dataset limits.** The authorized JSONL must hold at most 10,000 examples and
16 MiB in total (single records 64 KiB, fields 32 Ki characters, 4 Mi characters
overall). Zero examples aborts the run; fewer than 20 prints a warning that it
is a proof-of-pipeline run.

Each default run exports directly into a run-local immutable snapshot; an
explicit `SONDER_DATA` file is copied into that same snapshot boundary. The
trainer hashes the same byte stream it parses and never rereads mutable source
data for that run. The supported Qwen Hugging Face repositories are
pinned to reviewed 40-character commits (table in [Hugging Face
downloads](#3-hugging-face-downloads)), recorded in the plan/manifest, loaded
with `trust_remote_code=False`, and must match on resume.

### Training on a cloud GPU

`cloud_train.sh` trains on a rented Linux GPU box: it creates `.venv`, installs
torch (cu130 index) and `requirements-train.txt`, requires your
`training_data.jsonl` in the repository root (copy it from your machine; it is
gitignored on purpose), runs `adaptive_training.py start --confirm` with
`SONDER_DATA` pointing at it, and packages the adapter as a tarball. Its base
model defaults to the 7B (`SONDER_BASE`, default
`Qwen/Qwen2.5-Coder-7B-Instruct`) although its header says the base is selected
by Sonder Runtime. It keeps its state in `<repo>/.cloud-training-state.json`.
Optional Hub upload uses `HF_TOKEN` and `HF_REPO` (`<hf-user>/<repo>`).

**Limitation: an adapter trained elsewhere cannot be `deploy`ed locally.**
`deploy` accepts only the adapter bound to the local trusted training state
(`--adapter-dir` must resolve to that exact directory), and a tarball copied
from another machine has no such binding. The manual steps the script prints
(`convert_lora_to_gguf.py`, `ollama create`) bypass the evaluation canary, the
alias-ownership records and the recovery journal, so they are unsupported for
promotion into `sonder-personal:latest` or any routed alias. To use a cloud GPU
and still deploy, run the whole lifecycle (`start` then `deploy`) on one
machine. The cloud script is a training-only convenience.

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
   complete canary suite (the executable SQL tasks and the dynamic JSON
   instruction probe) through `sonder-personal:latest` and rejects any
   candidate-pass to published-alias-fail regression.
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

Quantization interaction: deployment applies the F16 GGUF adapter over the
Q4_K_M-quantized Ollama base, so adapter deltas land on already-quantized
weights at serve time. This is the intended trade — it preserves the pinned
base digest and avoids shipping a second multi-GB model — but it carries a
small fidelity cost versus the reference path (merge the adapter into the F16
base weights, then quantize the merged model once to Q4_K_M). If a candidate
that passed training-side evaluation shows quality regressions at serve time
that the training data does not explain, suspect this gap before suspecting
the adapter. Never stack the losses: do not fine-tune on top of
already-quantized weights, and do not quantize a model twice.

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

## Behavior status

Labels follow the [documentation status vocabulary](docs/architecture/DOCUMENT-AUTHORITY-INDEX.md#documentation-status-vocabulary). Unfinished
implementation work is tracked only in the
[master implementation specification](docs/architecture/SONDER-MASTER-IMPLEMENTATION-SPEC.md).

| Behavior | Status | Boundary |
|---|---|---|
| Attended QLoRA adapter training | Implemented | Explicit foreground command only; never started by bootstrap, Autopilot, cron, or a fleet (CORE-007). |
| Qwen adapter GGUF conversion, evaluation canary, and Ollama deployment | Implemented | Pinned llama.cpp commit and bounded held-out canary; not a general capability benchmark. |
| Native Windows bitsandbytes training | Degraded | Supported but less reliable than WSL2. |
| CPU offload during training (`--allow-cpu-offload`) | Unsupported | The current bitsandbytes/Trainer backend rejects it. |
| Full-parameter (dense) training start | Unsupported | `--full-finetune` produces a feasibility report only. |
| Importing arbitrary external adapters through `--adapter-dir` | Unsupported | Only the adapter bound to the current trusted training state is accepted. |
| Silent CPU training | Unsupported | Training refuses to fall back to CPU. |
| Local QLoRA without a supported NVIDIA CUDA runtime | Unsupported | The bitsandbytes path is disabled on other GPU vendors and CPU-only hosts. |
| Task, project, and personalization adapter catalog with explicit composition rules | Proposed | TRAIN-007; deterministic catalog compatibility checks are a foundation, not the complete contract. |
