# Runbook — assemble a routed model collection

Stand up a collection of specialist models and bind Sonder's tiers/lanes to them,
so the capability router sends each request to the best-suited model. Sized for a
**16 GB VRAM** GPU (e.g. RTX 5070 Ti); adjust per the
[Model Catalog](../wiki/18-model-catalog.md).

## Prerequisites

- Sonder installed and running against a reachable Ollama
  ([install-workstation-local](install-workstation-local.md)).
- Developer/admin auth for `/runtime` edits (a bare local-open loopback session
  is developer-authorized by default).
- Disk for the GGUFs (~30–40 GB for the set below) and the stated VRAM.

## 1. Pull the collection

```bash
ollama pull qwen2.5:3b               # fast  — router/titling/simple
ollama pull qwen2.5-coder:7b         # code  — agent-loop driver
ollama pull qwen2.5:14b              # general
ollama pull deepseek-r1:14b          # reasoning (fits 16 GB)
ollama pull qwen2.5vl:7b             # vision
ollama pull nomic-embed-text         # embedding (required for memory)
```

Portable/offline instead? Import a GGUF straight off a USB with
[use-facts-model](use-facts-model.md) (`setup_alias.py --from-usb`).

## 2. Bind tiers → models and lanes → tiers

```bash
# tiers -> concrete models
/runtime set fast=qwen2.5:3b code=qwen2.5-coder:7b general=qwen2.5:14b
# lanes -> tiers
/runtime set router=fast workbench=code autopilot=code fleet=code review=general
```

`reasoning`, `vision`, and `oracle` are optional tiers: add them to policy only
once the models are pulled. Until then the router degrades to `general`/`code`
automatically — nothing breaks. Inspect the live mapping with `/runtime` or
`runtime_policy_status()`.

## 3. Set residency (16 GB can't hold everything at once)

Keep the cheap interactive tiers warm and let the 14B specialists swap in:

```bash
export OLLAMA_KEEP_ALIVE=30m          # hold warm models for 30 min
# optional: allow a couple of small models resident together
export OLLAMA_MAX_LOADED_MODELS=2
```

Rule of thumb: `fast`(2.5) + `code`-7B(5.5) + `embed`(0.3) ≈ 8.5 GB stays
resident; the 14B `general`/`reasoning` and 7B `vision` load on demand.

## 4. Verify

```bash
python sonder_hardware.py             # confirms VRAM/band + whether speculation engages
python sonder_doctor.py               # config, policy, Ollama reachability, memory health
ollama ps                             # which models are currently resident
```

Then send one request of each kind and confirm the mode/tier line in the
response reports the expected tier:

- "hi" → `fast`
- "implement quicksort in Python" → `code`
- "prove step by step why merge sort is O(n log n)" → `reasoning` (or `general`
  if the reasoning tier isn't configured)
- "what's the latest news on X" → `fast` + web (if web tools enabled)

## 5. The oracle / escalation tier (optional, the hard 5%)

Pick one, honestly:

- **Big local** — a 32B (Q3 or CPU-offload on 16 GB; fully resident only at ≥24 GB
  VRAM) as a slow, private backstop: `/runtime set general=<32b-model>` for the
  heavy lane, `OLLAMA_KEEP_ALIVE=2h` to avoid re-paying its load.
- **Consented cloud** — set `SONDER_ALLOW_CLOUD=1` and route the `cloud-*` tier
  for the genuinely hard minority. Prompts leave the machine on that tier only;
  every other tier stays local.

The router only reaches `oracle` on an explicit escalation signal (a failed run,
an empty/low-confidence answer, or "think hard"), so the expensive path stays
rare.

## Rollback

`/runtime reset` restores safe defaults (all local base tiers). Pulled models
remain in Ollama's store; remove any with `ollama rm <model>`.

## Notes

- The policy can never enable cloud, widen permissions/roots, or store
  credentials ([Security Model](../wiki/09-security-model.md)) — it only selects
  local aliases and lanes. Cloud stays a separate host-owned consent gate.
- A larger GPU changes only the model sizes, not this procedure; see the upgrade
  table in the [Model Catalog](../wiki/18-model-catalog.md).
