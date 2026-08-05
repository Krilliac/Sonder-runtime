# Model Catalog — recommended models per job

A "collection of specialists" that Sonder routes between, so each request goes to
the best-suited model. The tiers here are exactly the ones the
[capability router](../../sonder_runtime/domain/routing/capability_router.py)
targets: `fast`, `code`, `general`, `reasoning`, `vision`, and (optional)
`oracle`. Sizes are approximate **VRAM** at Q4_K_M including a working context —
plan against VRAM, not system RAM ([Model Tiers & Gateway](08-model-tiers-and-gateway.md)).

> **Honest ceiling.** Routing sends each job to the right *local* specialist; it
> does not manufacture frontier capability. The smartest answer you get is only
> as smart as the strongest model in the collection. For the genuinely hard
> minority, the `oracle` tier is a big local model (needs the VRAM) or a
> **consented** cloud call — that is the realistic path to broad, Claude-like
> coverage, not a small model pretending.

## The 16 GB build (RTX 5070 Ti — the sweet spot)

At 16 GB VRAM you run **anything up to ~14B fully on the GPU**; 32B needs Q3 or
CPU offload (slower). Recommended collection:

| Tier | Job | Recommended model | ~VRAM | Why |
|---|---|---|---|---|
| `fast` | router, titling, simple Q&A | **Qwen2.5-3B-Instruct** | ~2.5 GB | instant; keeps the router itself cheap |
| `code` | workbench, autopilot, code gen | **Qwen2.5-Coder-7B-Instruct** (or `-14B`) | ~5.5 / ~10 GB | the driver of the agent loop; 7B holds the tool protocol, 14B is stronger |
| `general` | general chat, planning | **Qwen2.5-14B-Instruct** | ~10 GB | strongest all-rounder that still fits fully |
| `reasoning` | hard multi-step, math, design | **DeepSeek-R1-Distill-Qwen-14B** | ~10 GB | real reasoning-model behavior that *fits 16 GB* (QwQ-32B needs Q3/offload) |
| `vision` | image/screenshot/diagram input | **Qwen2.5-VL-7B-Instruct** | ~6.5 GB | reads images alongside text |
| *(embed)* | memory & recall (**required**) | **nomic-embed-text** (or `bge-m3`) | ~0.3 GB | powers semantic recall; not a chat model |
| *(rerank, optional)* | retrieval quality | **bge-reranker-v2-m3** | ~0.6 GB | second-pass filter for recall |
| `oracle` (optional) | the hard 5% | 32B @ Q3/CPU-offload, **or** consented cloud | see note | escalation backstop |

### Residency: you can't hold them all at once
16 GB won't keep three 14B models resident. The practical pattern:

- **Keep resident** (~8.5 GB, always warm): `fast` (2.5) + `code`-7B (5.5) + `embed` (0.3).
- **Swap on demand**: the 14B `general`/`reasoning` and the 7B `vision` (Ollama
  unloads the coder to make room). Tune with `OLLAMA_KEEP_ALIVE`; the
  [prewarm](11-speculation-and-prediction.md) path hides some of the reload.
- Coding all day? Instead keep **Coder-14B** resident and let the rest swap.

The router's job is to make those swaps *worth it* — a small `fast` model answers
trivia without ever waking a 14B, and only a real reasoning task pays the reload.

## Wiring it into Sonder

Pull the collection, then bind lanes to tiers (developer/admin auth):

```bash
ollama pull qwen2.5:3b
ollama pull qwen2.5-coder:7b
ollama pull qwen2.5:14b
ollama pull deepseek-r1:14b
ollama pull qwen2.5vl:7b
ollama pull nomic-embed-text
# bind tiers -> models and lanes -> tiers
/runtime set fast=qwen2.5:3b code=qwen2.5-coder:7b general=qwen2.5:14b
/runtime set workbench=code autopilot=code review=general
```

`reasoning`/`vision`/`oracle` become live tiers once the operator adds them to
policy; until then the router degrades to `general`/`code` automatically. Full
procedure: [assemble-model-collection](../runbooks/assemble-model-collection.md).

## When you upgrade VRAM later

| VRAM | Unlocks |
|---|---:|
| 16 GB (5070 Ti) | up to 14B fully; 32B via Q3/offload |
| 24 GB (3090/4090/5080-class) | **32B fully** (QwQ-32B, Coder-32B) — the reasoning/coding jump |
| 48 GB (2×24 / RTX 6000) | **70B** — a real local `oracle` tier |
| 64–128 GB unified (Mac/DGX) | 70B+ / big MoE |

Your **9900X3D + 32 GB DDR5-6000** also lets a 32B spill partially to system RAM
for a slow-but-usable local `oracle` (good for overnight/batch distillation, per
the teacher→student loop in [Training](15-training.md)) — while the GPU keeps the
interactive tiers fast.

## How the router chooses (the brain)

`capability_router` classifies each request into a task class and picks the tier,
with an escalation ladder for when the first choice isn't enough:

| Task class | First tier | Escalation ladder (→ oracle if consented) |
|---|---|---|
| simple / trivia | `fast` | fast → general → reasoning |
| current-info / search | `fast` + web | fast → general |
| code | `code` | code → general → reasoning → *oracle* |
| reasoning / math / design | `reasoning` | reasoning → general → code → *oracle* |
| vision | `vision` | vision → general |
| long-context (large payload) | `general` | general → reasoning → *oracle* |

Escalation fires only on a real signal — a failed run, an empty/low-confidence
answer, or an explicit "think hard" — so a satisfied cheap answer never wastes a
14B. See [Agent, Autopilot & Fleet](07-agent-autopilot-fleet.md) for how the loop
consumes these decisions.
