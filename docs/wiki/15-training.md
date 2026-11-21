# Training

Sonder can personalize a local model on **your** context by training LoRA/
QLoRA adapters from grounded data, evaluating them, and promoting only
validated results. Training is isolated from serving, and every run is an explicit, attended
`start --confirm`. What gates a run (and what does not) is in
[TRAINING.md](../../TRAINING.md#commands). To set up a machine for
training (hardware, training venv, Hugging Face, llama.cpp, a remote or
Caddy-fronted Ollama, WSL2), follow
[TRAINING.md#set-up-a-training-host](../../TRAINING.md#set-up-a-training-host);
every variable is in the
[environment reference](../../TRAINING.md#environment-reference).

## Where the data comes from

The learning loop ([Memory & Learning](06-memory-and-learning.md)) captures
interactions and prices real outcomes. Interactions with good outcomes
(compiled, tests passed, accepted) are the training signal — so the model
learns from work that actually worked, including grounded wins from a
cloud/teacher tier. `export_training_data.py` builds the dataset and
cross-checks each row's stored reward against the canonical pricing to
detect corruption.

## Pipeline

1. **Dataset** — a fresh grounded export into each run's directory
   (`start` does this itself).
2. **Train** — LoRA/QLoRA via PEFT/Hugging Face (`qlora_train.py`, driven by
   `adaptive_training.py`); attended, resource-bounded. Commands, resume rules
   and dataset limits: [TRAINING.md](../../TRAINING.md#commands).
3. **Evaluate** — the candidate adapter is scored on a bounded held-out canary
   (`promotion_eval.py`).
4. **Promote (gated)** — only a validated adapter is deployed to Ollama, as a
   recoverable, journaled transition so a client never sees a half-applied
   model. Steps: [TRAINING.md](../../TRAINING.md#qwen-adapter-deployment).
5. **Rollback** — `/training rollback` (or `python adaptive_training.py
   rollback`) points code/general back at `sonder:latest`; a failed evaluation
   leaves the prior alias and policy active. See
   [TRAINING.md](../../TRAINING.md#rollback).

## Safety & isolation

- Training and deploy/rollback take file locks (the training-lifecycle lock in
  the state home, the runtime-policy and training-state lifecycle locks, and a
  per-user lock for the Ollama alias), so two lifecycle commands cannot run at
  once; a second one fails immediately with `another training lifecycle
  operation is already running`. The locks are not the SPEC-2 maintenance locks
  and carry no TTL: they are released when the holding process exits. Details
  and diagnosis: [training-failure](../runbooks/training-failure.md).
- A failed training run never changes the serving alias or runtime policy.
- The reserved `sonder-personal:latest` alias is the personalization
  target; the stable `sonder:latest` alias keeps serving throughout.
- Deploy, promotion and rollback need a loopback Ollama; see
  [TRAINING.md](../../TRAINING.md#remote-or-caddy-fronted-ollama).
- Operational procedure: [training-failure](../runbooks/training-failure.md).

## Related surfaces

- Endless/curriculum practice: `/train [N]`, `endless_train.py`,
  `curriculum_run.py`, `self_curriculum.py`.
- Runtime policy selects which local aliases and lanes are live
  ([Model Tiers & Gateway](08-model-tiers-and-gateway.md)); training
  produces new aliases for it to select once promoted.
- NPU acceleration for routing/embeddings is a separate utility path
  ([NPU.md](../../NPU.md)), never a generative tier.
