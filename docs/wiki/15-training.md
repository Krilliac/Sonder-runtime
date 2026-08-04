# Training

Sonder can personalize a local model on **your** context by training LoRA/
QLoRA adapters from grounded data, evaluating them, and promoting only
validated results. Training is isolated from serving and is off by default
(`[features].training = false`). Deep dive: [TRAINING.md](../../TRAINING.md).

## Where the data comes from

The learning loop ([Memory & Learning](06-memory-and-learning.md)) captures
interactions and prices real outcomes. Interactions with good outcomes
(compiled, tests passed, accepted) are the training signal — so the model
learns from work that actually worked, including grounded wins from a
cloud/teacher tier. `export_training_data.py` builds the dataset and
cross-checks each row's stored reward against the canonical pricing to
detect corruption.

## Pipeline

1. **Dataset** — grounded interactions/lessons exported to a training set.
2. **Train** — LoRA/QLoRA via PEFT/Hugging Face (`qlora_train.py`,
   `adaptive_training.py`); attended, resource-bounded.
3. **Evaluate** — the candidate adapter is scored against held-out /
   grounded checks (`promotion_eval.py`, `eval_*`).
4. **Promote (gated)** — only a validated adapter is deployed to Ollama.
   Promotion is an atomic, saga-style transition (reserve → validate →
   alias update → policy update → verify → commit) so a client never sees
   a half-applied model.
5. **Rollback** — a first-class use case; a failed evaluation leaves the
   prior alias and policy active, and rollback identity is verified.

## Safety & isolation

- Training and promotion take SPEC-2 maintenance locks, so they cannot run
  concurrently with a backup, restore, or update.
- A failed training run never changes the serving alias or runtime policy.
- The reserved `sonder-personal:latest` alias is the personalization
  target; the stable `sonder:latest` alias keeps serving throughout.
- Operational procedure: [training-failure](../runbooks/training-failure.md).

## Related surfaces

- Endless/curriculum practice: `/train [N]`, `endless_train.py`,
  `curriculum_run.py`, `self_curriculum.py`.
- Runtime policy selects which local aliases and lanes are live
  ([Model Tiers & Gateway](08-model-tiers-and-gateway.md)); training
  produces new aliases for it to select once promoted.
- NPU acceleration for routing/embeddings is a separate utility path
  ([NPU.md](../../NPU.md)), never a generative tier.
