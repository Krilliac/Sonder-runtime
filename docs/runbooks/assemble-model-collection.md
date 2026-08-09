# Runbook — assemble a routed model collection

Stand up a collection of specialist models and bind Sonder's tiers/lanes to them,
so the capability router sends each request to the best-suited model. Model sizes
are chosen from your hardware's band — see the
[Model Catalog](../wiki/18-model-catalog.md) for the band table and the model
families that fill each role.

## Prerequisites

- Sonder installed and running against a reachable Ollama
  ([install-workstation-local](install-workstation-local.md)).
- Developer/admin auth for `/runtime` edits (a bare local-open loopback session
  is developer-authorized by default).
- Disk for the model files and enough VRAM (or unified/system memory) for the
  band you are targeting.

## 1. Size the collection to the machine

```bash
python sonder_hardware.py          # cross-vendor inventory + resident/hybrid plans
```

Use the reported band with the [Model Catalog](../wiki/18-model-catalog.md)
tables to choose one model per role. A minimal viable collection is **three**
models: a `fast` router model, one `code`/`general` workhorse, and an
**embedding** model (required for memory/recall). Add `reasoning` and `vision`
only if the band supports keeping or swapping them.

The report keeps the largest single accelerator separate from auxiliary GPUs
and integrated graphics. It never sums mixed-vendor memory or treats an OS
enumeration as proof that CUDA, ROCm, Vulkan, Metal, or another backend works.
Use `ollama ps` and a real inference smoke test for runtime proof. A hybrid
class is a capacity estimate: it reserves system headroom and assumes slow
CPU/unified-memory spill, so prefer the resident class for interactive work.

## 2. Pull the collection

```bash
ollama pull <fast-model>          # small general instruct  -> fast
ollama pull <coder-model>         # code-specialized        -> code
ollama pull <general-model>       # mid-size general        -> general
ollama pull <embedding-model>     # embeddings (required)
# optional, band permitting:
ollama pull <reasoning-model>     # reasoning/"thinking"    -> reasoning
ollama pull <vision-model>        # vision-language         -> vision
```

Portable/offline instead? Import a GGUF directly (including from a USB) with
[use-facts-model](use-facts-model.md) (`setup_alias.py --from-usb`).

## 3. Bind tiers → models and lanes → tiers

```bash
# tiers -> concrete models
/runtime set fast=<fast-model> code=<coder-model> general=<general-model>
# lanes -> tiers
/runtime set router=fast workbench=code autopilot=code fleet=code review=general
```

`reasoning` and `vision` are first-class optional tiers in the policy. They are
unbound by default; bind models suited to the live host with
`/runtime set reasoning=<model> vision=<model>`, or leave one **unset** with an
empty value (`/runtime set vision=`). An
unset tier is not offered at all and the router degrades to `general`/`code`
automatically — nothing breaks. `oracle` is still consent-gated escalation
only, not a policy tier. Inspect the live mapping with `/runtime` or
`runtime_policy_status()`.

## 4. Set residency

Keep the small, constantly-used tiers warm and let the large specialists swap in:

```bash
export OLLAMA_KEEP_ALIVE=30m          # hold warm models longer
export OLLAMA_MAX_LOADED_MODELS=2     # if memory allows more than one resident
```

Rule of thumb: `fast` + `embed` + your most-used heavy tier stay resident; the
rest load on demand. On a multi-accelerator machine you may instead pin one
model per device when its backend supports that (for example,
`CUDA_VISIBLE_DEVICES` on NVIDIA) to eliminate swapping entirely — see
"Scaling up: multi-GPU" in the [Model Catalog](../wiki/18-model-catalog.md).

## 5. Verify

```bash
python sonder_hardware.py             # inventory, fit classes, speculation guidance
python sonder_doctor.py               # config, policy, Ollama reachability, memory health
ollama ps                             # which models are currently resident
```

Then send one request of each kind and confirm the reported mode/tier:

- "hi" → `fast`
- "implement quicksort in Python" → `code`
- "prove step by step why merge sort is O(n log n)" → `reasoning` (or `general`
  if no reasoning tier is configured)
- "what's the latest news on X" → `fast` + web (if web tools are enabled)

## 6. The oracle / escalation tier (optional)

For the genuinely hard minority, pick one:

- **Large local model** — only if the memory band supports it (a 70B at Q4 needs
  roughly 48 GB). Point the heavy lane at it and raise `OLLAMA_KEEP_ALIVE` so its
  long load is not re-paid. Private, slow.
- **Consented cloud** — set `SONDER_ALLOW_CLOUD=1` and route the `cloud-*` tier.
  Prompts leave the machine on that tier only; every other tier stays local.

The router reaches `oracle` only on an explicit escalation signal (a failed run,
an empty/low-confidence answer, or "think hard"), so the expensive path stays
rare.

## Rollback

`/runtime reset` restores safe defaults (all local base tiers). Pulled models
remain in Ollama's store; remove any with `ollama rm <model>`.

## Notes

- Policy can never enable cloud, widen permissions/roots, or store credentials
  ([Security Model](../wiki/09-security-model.md)) — it only selects local
  aliases and lanes. Cloud remains a separate host-owned consent gate.
- Changing hardware changes only the model sizes, not this procedure; re-run
  `sonder_hardware.py` and re-pick from the band table.
