# Runbook — assemble a routed model collection

Stand up a collection of specialist models and bind Sonder's tiers/lanes to them,
so the capability router sends each request to the best-suited model. Model sizes
are chosen from your hardware's band — see the
[Model Catalog](../wiki/18-model-catalog.md) for the band table and the model
families that fill each role.

## Prerequisites

- Sonder installed and running against a reachable Ollama
  ([install-workstation-local](install-workstation-local.md)).
- Admin auth for `/runtime` edits (a bare local-open loopback session
  is admin-authorized by default).
- Disk for the model files and enough VRAM (or unified/system memory) for the
  band you are targeting.

## 1. Size the collection to the machine

```bash
python sonder_hardware.py          # cross-vendor inventory + resident/hybrid plans
```

Use the reported band with the [Model Catalog](../wiki/18-model-catalog.md)
tables to choose models per role. The minimum for normal local chat is **one**
generative model: `fast`, `code`, and `general` may all share it. Add an
**embedding** model when you want semantic memory/recall; bootstrap installs
one by default, but chat remains available without it. Add `reasoning` and
`vision` only if the band supports keeping or swapping them.

For an intentionally chat-only bootstrap, run
`python setup_alias.py --no-embedding`; `/runtime status refresh` will show semantic memory as
unavailable until you install and bind an embedding model.

The report keeps the largest single accelerator separate from auxiliary GPUs
and integrated graphics. It never sums mixed-vendor memory or treats an OS
enumeration as proof that CUDA, ROCm, Vulkan, Metal, or another backend works.
Use `ollama ps` and a real inference smoke test for runtime proof. A hybrid
class is a capacity estimate: it reserves system headroom and assumes slow
CPU/unified-memory spill, so prefer the resident class for interactive work.

## 2. Pull the collection

```bash
ollama pull <base-model>          # may serve fast + code + general
ollama pull <embedding-model>     # optional for core chat; enables semantic memory
# optional, band permitting:
ollama pull <fast-model>          # separate low-latency router model
ollama pull <coder-model>         # separate code specialist
ollama pull <general-model>       # separate general specialist
ollama pull <reasoning-model>     # reasoning/"thinking"    -> reasoning
ollama pull <vision-model>        # vision-language         -> vision
```

Portable/offline instead? Import a GGUF directly (including from a USB) with
[use-facts-model](use-facts-model.md) (`setup_alias.py --from-usb`).

## 3. Bind tiers → models and lanes → tiers

```bash
# tiers -> concrete models
/runtime set fast=<base-model> code=<base-model> general=<base-model>
/runtime set embedding=<embedding-model>
# lanes -> tiers
/runtime set router=fast workbench=code autopilot=code fleet=code review=general
```

When you later install separate specialist models, replace only the role you
want to specialize, for example `/runtime set code=<coder-model>` or
`/runtime set fast=<fast-model>`. Do not bind a model name before it is
installed: runtime policy deliberately rejects unavailable local models.

`reasoning` and `vision` are first-class optional tiers in the policy. They are
unbound by default; bind models suited to the live host with
`/runtime set reasoning=<model> vision=<model>`, or leave one **unset** with an
empty value (`/runtime set vision=`). An
unset tier is not offered at all and the router degrades to `general`/`code`
automatically — nothing breaks. `oracle` is not wired into live escalation (automatic escalation stays on
bound local tiers; hosted `cloud-*` tiers are explicit picks after
`SONDER_ALLOW_CLOUD=1`); it is a router name
only, not a policy tier. Inspect the live mapping with `/runtime` or
`runtime_policy_status()`.

## 4. Set residency

Keep the small, constantly-used tiers warm and let the large specialists swap in:

```bash
export SONDER_KEEP_ALIVE=30m          # hold warm generative models longer
export SONDER_EMBED_KEEP_ALIVE=30m    # hold the embedder warm too
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

- "hi" → `general` (the `chat` lane's default tier)
- "implement quicksort in Python" → `general` (chat lane; a workspace request such as "implement a quicksort module in Python" → `workbench` / `code`)
- "prove step by step why merge sort is O(n log n)" → `reasoning` (or `general`
  if no reasoning tier is configured)
- "what's the latest news on X" → `code` + web (if web tools are enabled)

## 6. The oracle / escalation tier (optional)

For the genuinely hard minority, pick one:

- **Large local model** — only if the memory band supports it (a 70B at Q4 needs
  roughly 48 GB). Bind it to a ladder tier such as `reasoning` and raise
  `SONDER_KEEP_ALIVE` so its long load is not re-paid. Private, slow. A tier on
  the ladder is an automatic escalation rung (see below).
- **Consented cloud** — set `SONDER_ALLOW_CLOUD=1` and pick a `cloud-*` tier;
  setup is in [Hosted cloud tiers](#hosted-cloud-tiers-optional) below.
  Prompts leave the machine on that tier only; every other tier stays local.

Automatic escalation never reaches a `cloud-*` tier: after a failed run, an
empty answer, or runnable code that still fails verification after a repair, it
steps up only through locally bound tiers. The `cloud-*` tiers (`cloud-code`,
`cloud-general`) run only when you pick them explicitly after
`SONDER_ALLOW_CLOUD=1`. `oracle` is not reachable at all, automatically or by
choice: the current runtime never offers it (see above). A large local model
bound to a ladder tier such as `reasoning` is different: it is an automatic
escalation rung, so a failed or empty attempt on a lower tier can reach it. Bind
it to `reasoning` only if that is acceptable.

## Hosted cloud tiers (optional)

This section is the one home for cloud-tier setup (checked 2026-11-20). The
tiers are `cloud-code` and `cloud-general`; they run on Ollama's hosted
service, need no local weights, and are never a startup requirement.

1. **Consent.** Set `SONDER_ALLOW_CLOUD` to `1` (`true`, `yes` and `on` also
   count) in the environment of every process that should be allowed to use
   them:

   ```powershell
   $env:SONDER_ALLOW_CLOUD = '1'     # this PowerShell session only
   ```

   ```bash
   export SONDER_ALLOW_CLOUD=1       # this shell only
   ```

   Without it a cloud request is refused before anything leaves the machine
   with `hosted/cloud tiers are disabled. Set SONDER_ALLOW_CLOUD=1 to opt in;
   prompts sent to cloud tiers leave this machine.`, and `/model` and
   `/v1/models` do not offer the tiers.
2. **Models.** The default model ids are listed in the
   [tier table](../wiki/08-model-tiers-and-gateway.md#remote--cloud-opt-in)
   (authoritative source: `default_cloud_models` in
   `sonder_runtime/adapters/data/model_lists.json`). Override either with
   `SONDER_CLOUD_CODE` / `SONDER_CLOUD_GENERAL`. A value naming a retired cloud
   model (`qwen3-coder:480b-cloud`, `deepseek-v4-flash:cloud`) is ignored with a
   logged warning and the default is used instead.
3. **Use a host Ollama, not the bundled one.** `SONDER_ALLOW_CLOUD` only gates
   Sonder. When the runtime uses a sealed engine bundle that ships Ollama, the
   launchers (`sonder-runtime.sh`, `sonder-runtime.cmd`) set `OLLAMA_NO_CLOUD=1`
   for that Ollama, and Ollama's own setting turns off cloud models
   ([Ollama FAQ](https://docs.ollama.com/faq)). When the bundled Ollama is the
   one serving, the tiers are listed but every cloud request fails at Ollama. If
   a host Ollama is already answering, bootstrap uses it instead
   (`bootstrap_engine.py:159-160`) and the launcher's `OLLAMA_NO_CLOUD` does not
   apply to that process. For cloud tiers, run Sonder without the
   bundle (a source checkout with no `engine/` bundle and no
   `SONDER_ENGINE_BUNDLE`) against a host Ollama that is signed in to Ollama's
   hosted service.
4. **Verify with a real request.** In the REPL, `/model` now lists `cloud-code`
   and `cloud-general`; `/model cloud-code` selects one for the session. Then
   send one short prompt such as `Say hello in one short sentence.`
   Expected: a normal answer. If it instead fails with a provider error from
   Ollama (the wording is Ollama's, not Sonder's) while `/model` still lists the
   tier, cloud is off or not signed in at the Ollama you are using; check
   step 3. Selecting is a deliberate act: automatic escalation never reaches a
   cloud tier (section 6). Listing alone does not prove cloud works.
5. **Rollback.** Unset `SONDER_ALLOW_CLOUD` (or set it to `0`) and restart the
   process; the tiers disappear again. To revoke consent at once without a
   restart, call the process-local `cloud_opt_in` MCP tool with
   `action="off"` ([consent paths](../wiki/08-model-tiers-and-gateway.md#remote--cloud-opt-in)).
   Nothing is stored in the runtime policy (it cannot enable cloud).

Account sign-in with Ollama's hosted service is done with Ollama itself and is
not driven or checked by Sonder; this runbook does not cover it.

Other hosted providers are separate gateways with their own keys, spend caps
and consent steps: [OpenRouter](../integration/openrouter.md) and
[Anthropic](../integration/anthropic.md). Remote (non-cloud) Ollama is a
different gate:
[remote-ollama-caddy](remote-ollama-caddy.md#coordinator-settings).

## Rollback

`/runtime reset` restores safe defaults (all local base tiers). Pulled models
remain in Ollama's store; remove any with `ollama rm <model>`.

## Notes

- Policy can never enable cloud, widen permissions/roots, or store credentials
  ([Security Model](../wiki/09-security-model.md)) — apart from `provider_models`,
  which names the model an already-bound, already-consented **OpenRouter** tier
  serves (only the `openrouter` section is read), it only selects local
  aliases and lanes. Anthropic does not use `provider_models`: its model comes
  from an explicit request `model` option, else `SONDER_ANTHROPIC_TIER_MODELS`
  (`tier=model,...`), else `SONDER_ANTHROPIC_MODEL`, else a built-in default
  (see [Anthropic](../integration/anthropic.md) for the current one). Cloud
  remains a separate host-owned consent gate.
- Changing hardware changes only the model sizes, not this procedure; re-run
  `sonder_hardware.py` and re-pick from the band table.
