# Model Catalog — recommended models per job

A "collection of specialists" that Sonder routes between, so each request goes to
the best-suited model. The tiers here are exactly the ones the
[capability router](../../sonder_runtime/domain/routing/capability_router.py)
targets: `fast`, `code`, `general`, `reasoning`, `vision`, and (optional)
`oracle`.

Sizes throughout are approximate **VRAM at Q4_K_M including working context**.
Plan against **VRAM**, not system RAM — that is the number that decides what you
can run ([Model Tiers & Gateway](08-model-tiers-and-gateway.md)). CPU-only and
unified-memory machines are covered at the end.

> **Honest ceiling.** Routing sends each job to the right *local* specialist; it
> does not manufacture frontier capability. The smartest answer you get is only
> as smart as the strongest model in the collection. For the genuinely hard
> minority, the `oracle` tier is a large local model (needs the VRAM) or a
> **consented** cloud call — that is the realistic path to broad coverage, not a
> small model pretending.

## 1. What is actually required

Sonder does not require a complete specialist collection to start. The smallest
useful local installation is **Ollama plus one generative local model**, exposed
through the stable `sonder:latest` alias. That one model may serve the `fast`,
`code`, and `general` roles. Bootstrap and `setup_alias.py` additionally pull
the default embedding model (`nomic-embed-text`) so semantic memory works on a
fresh installation, but embedding is not a prerequisite for ordinary chat.

| Capability | Required for core chat? | When it becomes required |
|---|---|---|
| One local generative model (`sonder:latest`) | **Yes** | Normal local REPL/API chat and code work |
| Embedding model (`nomic-embed-text`, BGE, E5, mxbai) | No | Semantic memory, lesson recall, and vector search |
| Separate `fast`, `code`, or `general` models | No | Only when you want different quality/latency tradeoffs; one model can fill all three |
| `reasoning` model | No | Explicit hard-reasoning routing |
| `vision` model | No | Local `/vision` image analysis |
| Reranker, extraction, tool-oriented, speech, or experimental models | No | Only after their matching integration is enabled and configured |
| Cloud model | No | Explicit cloud-tier work; it is separately consent-gated |

An unbound optional routing tier degrades to a configured local `general` or
`code` path; it is not a startup failure. A missing embedding model disables
semantic retrieval rather than core chat.

## 2. Find your band

| VRAM | Largest comfortable model | What the collection looks like |
|---|---|---|
| **8 GB** | 7–8B at Q4 | one general/coder model + embeddings; tiers share it |
| **12 GB** | 12–14B at Q4 | small `fast` + a 7B `code`, 14B swaps in |
| **16 GB** | 14B at Q4 fully | full base collection; 32B only at Q3/offload |
| **24 GB** | **32B at Q4** | adds a real `reasoning` tier — the biggest single jump |
| **32 GB** | 32B at Q6–Q8 | comfortable 32B + a second model resident |
| **48 GB** | **70B at Q4** | a genuine local `oracle` tier |
| **64 GB+** unified/multi-GPU | 70B+ / large MoE | oracle plus everything else warm |

Everything below is expressed per-band, so pick your row and read across.

## 3. The collection by role

| Tier | Job | 8–12 GB | 16 GB | 24–32 GB | 48 GB+ |
|---|---|---|---|---|---|
| `fast` | router, titling, simple Q&A | 1.5–3B | 3B | 3–4B | 4–8B |
| `code` | workbench, autopilot, code gen | 7B coder | 7B coder | **14–32B coder** | 32B coder |
| `general` | general chat, planning | 7–8B | 14B | 14–32B | 70B |
| `reasoning` | hard multi-step, math, design | *(fold into general)* | 14B reasoning | **32B reasoning** | 70B reasoning |
| `vision` | image / screenshot input | 3B VL | 7B VL | 7–8B VL | 8B+ VL |
| *(embed)* | semantic memory & recall | small embed | small embed | small embed | small embed |
| `oracle` | the hard 5% | consented cloud | consented cloud | 70B @ Q3 / cloud | **70B local** |

**Model families that fill these roles well** (any Ollama-available equivalent
works — Sonder is model-agnostic):

- **fast / router** — small general instruct models (Qwen2.5 3B, Llama 3.2 3B, Gemma 3 4B).
- **code** — code-specialized instruct models (Qwen2.5-Coder, DeepSeek-Coder, Codestral).
- **general** — mid-size general instruct models (Qwen2.5, Llama 3.x, Mistral Small, Gemma 3).
- **reasoning** — reasoning/"thinking" models (DeepSeek-R1 distills, QwQ). They emit
  long internal chains — slower, much better on multi-step work.
- **vision** — vision-language models (Qwen2.5-VL, Llama Vision, Gemma 3 multimodal).
- **embed** — embedding models (nomic-embed-text, BGE, E5, mxbai). **Required** for
  semantic memory/recall; not a chat model and not required for core chat.
- **rerank** *(optional)* — reranker models (bge-reranker) to sharpen retrieval.

Abliterated variants of any of the above trade the model's built-in refusals for
fewer false-refusals on legitimate dual-use work; Sonder's host guardrails are
enforced independently of the model ([Security Model](09-security-model.md)).

## 4. Residency: you usually cannot hold them all

Ollama keeps a limited set of models resident and swaps the rest on demand, so
routing between tiers can cost a reload. The pattern that works at any size:

- **Keep resident:** `fast` + `embed` + whichever heavy tier you use most
  (usually `code`). These are small and answer constantly.
- **Swap on demand:** the larger `general`/`reasoning` and `vision` models.
- Tune with `OLLAMA_KEEP_ALIVE` (hold warm longer) and `OLLAMA_MAX_LOADED_MODELS`.
  The [prewarm path](11-speculation-and-prediction.md) hides part of the reload.

The router's job is to make swaps *worth it*: a small `fast` model answers trivia
without ever waking a large model, and only a genuinely hard task pays the cost.

## 5. Wiring it into Sonder

```bash
ollama pull <fast-model>
ollama pull <coder-model>
ollama pull <general-model>
ollama pull <embedding-model>
# bind tiers -> models, lanes -> tiers (developer/admin auth)
/runtime set fast=<fast-model> code=<coder-model> general=<general-model>
/runtime set embedding=<embedding-model>
/runtime set router=fast workbench=code autopilot=code review=general
```

`reasoning` and `vision` are live policy tiers, bound by default and
repointable with `/runtime set reasoning=<model> vision=<model>`; assign an
empty value to leave one unset on a smaller collection, and the router degrades
to `general`/`code` automatically — nothing breaks. `oracle` remains
consent-gated escalation, not a policy tier. Full procedure:
[assemble-model-collection](../runbooks/assemble-model-collection.md).

`embedding` is deliberately a separate local-only binding, not a chat tier.
Sonder accepts it only when the live Ollama catalog declares embedding
capability. Changing it affects **future** vectors; stored lessons and sessions
keep their original model/revision provenance until the operator explicitly runs
`/embeddings apply` to refresh them. This prevents a model swap from silently
mixing incompatible vector spaces.

### Local image analysis

After binding an installed VLM that explicitly declares the `vision`
capability, analyze a guarded local image without sending its pixels to a cloud
provider:

```text
/runtime set vision=qwen2.5vl:3b
/contextsize 4k
/vision path/to/screenshot.png | What is the visible error and likely cause?
```

`vision_analyze(path, prompt)` is also available to direct MCP callers. It
accepts PNG, JPEG, and BMP up to 8 MiB, checks the normal file-root and
secret/control-plane read policy, requires a loopback Ollama endpoint, and
refuses an unconfigured, cloud, or non-vision target. Image text is explicitly
treated as untrusted data; it cannot issue tools or override the requested
analysis. It needs at least 4k of the operator-selected native context and
does not silently widen the context/VRAM policy. This initial surface is
direct/REPL only—agents do not receive an
image channel until their local-input transcript contract is separately wired.

## 6. Scaling up: multi-GPU

Multi-GPU LLM inference does **not** use SLI or NVLink — those are irrelevant
here. Frameworks split the model by **layers** across cards and pass a small
activation tensor over ordinary PCIe, so a modest slot (x4/x8) is fine; PCIe
bandwidth mostly affects load time, not per-token speed.

**Two rules decide whether a pairing works:**

1. **Do not mix vendors for one model.** CUDA and oneAPI/SYCL (and ROCm) are
   separate backends; a single model cannot be split across an NVIDIA and an
   Intel/AMD card. A mixed pair realistically means two *independent* model
   servers, not pooled VRAM.
2. **Bandwidth sets token speed.** Generation is memory-bandwidth-bound, so a
   card's GB/s matters as much as its capacity. A high-capacity but low-bandwidth
   card generates proportionally slower.

Mixing *generations* from the **same** vendor is fine — layers split across
different-sized cards proportionally.

**Two ways to use a second card**, and the second is often the better daily win:

- **Pool** — split one large model across both for a bigger `oracle`/`reasoning`
  tier.
- **Pin one model per card** (e.g. `CUDA_VISIBLE_DEVICES`) — keep the coder warm
  on one and the reasoning model warm on the other. **Zero swap latency**, which
  suits a routed collection of specialists better than one giant model.

Practical checks before buying: PSU headroom for two cards, case clearance and
airflow, and enough PCIe slots. Aim at a **capacity target**, not a card count —
reaching 48 GB (for a 70B `oracle`) means two 24 GB cards, which is worth knowing
before buying a second smaller one.

## 7. Accelerators: NPUs and TPUs

**Neither an M.2/USB NPU nor a TPU can run an LLM.** They have no usable memory
for multi-GB weights and target small quantized CNNs. Every tier above runs on
GPU or CPU. What they *can* serve is Sonder's **utility path** — the bounded
routing and embedding work that sits *below* the model tiers ([NPU.md](../../NPU.md)).

| Class | Examples | Runtime | Usable today |
|---|---|---|---|
| **NPU** (integrated) | AMD XDNA (Ryzen AI), Intel NPU, Qualcomm HTP | onnxruntime EPs (`vitisai`/`openvino`/`qnn`) | **Yes** — routing + embeddings |
| **TPU** (M.2/USB) | Google Coral, Hailo-8/8L | libedgetpu/TFLite, HailoRT/HEF | **No** — descriptor-only |

TPU-class devices are declared and reported honestly (`edgetpu`, `hailo`) but are
**descriptor-only**: neither ships an onnxruntime execution provider, so the
resolver can never select one and the utility path stays on its local fallback.
Coral additionally cannot hold a transformer embedding model in its on-chip SRAM.
Detection states this rather than staying silent.

**When an accelerator is worth it:** a low-power, always-on machine with **no
capable GPU**, where routing/embeddings should run without waking a big card. If
the machine already has a discrete GPU, an add-in NPU/TPU is redundant — the GPU
runs embeddings far faster than a few-TOPS INT8 device, and the router model is
small enough to be nearly free. Note also that integrated NPUs ship mainly in
laptop/APU parts; most desktop CPUs have none, so those provider paths simply do
not apply there.

## 8. No discrete GPU?

- **CPU-only** — workable with 3–8B models; expect seconds-per-response. Keep the
  collection small (one general/coder model + embeddings) and prefer the
  foreground workbench over long autopilot runs.
- **Unified memory (Apple Silicon and similar)** — the whole memory pool is
  usable, so large models run well; size by total unified RAM using the band
  table above.
- **System-RAM offload** — a model larger than VRAM can spill to system RAM at a
  large speed penalty. Useful for a slow, private `oracle` used for
  overnight/batch work (see the teacher→student loop in [Training](15-training.md)),
  not for interactive use.
- **Auxiliary iGPU/GPU** — keep an integrated or second-vendor adapter on
  displays, or verify it independently for a separate embedding, routing, or
  draft-model service. Do not assume unlike adapters can pool memory for one
  model; the backend and topology decide that.

## 9. How the router chooses

`capability_router` classifies each request and picks a tier, with an escalation
ladder for when the first choice is not enough:

| Task class | First tier | Escalation ladder (→ oracle if consented) |
|---|---|---|
| simple / trivia | `fast` | fast → general → reasoning |
| current-info / search | `fast` + web | fast → general |
| code | `code` | code → general → reasoning → *oracle* |
| reasoning / math / design | `reasoning` | reasoning → general → code → *oracle* |
| vision | `vision` | vision → general |
| long-context (large payload) | `general` | general → reasoning → *oracle* |

The operator's lane→tier mapping wins for ordinary work; the router only upgrades
to a **specialist** tier that has actually been configured. Escalation fires only
on a real signal — a failed run, an empty/low-confidence answer, or an explicit
"think hard" — so a satisfied cheap answer never wastes a large model. See
[Agent, Autopilot & Fleet](07-agent-autopilot-fleet.md) for how the loop consumes
these decisions.
