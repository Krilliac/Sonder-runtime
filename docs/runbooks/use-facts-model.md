# Use a portable GGUF model (facts. USB)

Sonder is model-agnostic: any open-weight GGUF that Ollama can load works
as a tier. This runbook covers importing a portable model such as an
**Open Source Everything "facts." USB** (Qwen 3.x 4B, llama.cpp) and
wiring it into Sonder. The same steps work for any `.gguf` file.

Nothing here contacts a registry — it is a fully offline import, matching
both Sonder's private-first posture and the facts. device's offline
design.

## What you are plugging in

`facts.` ships a Qwen 3.x 4B "abliterated" model as a `.gguf`, run by
llama.cpp. In Sonder terms that is a solid **`fast`/`general` tier** and a
light **`code`** tier: measurably more capable than a 1.5B (it can drive
the agent tool-loop far more often) but not as reliable as a 7B for long
multi-step autopilot work. Pick tiers accordingly (see step 4).

> **Guardrails note.** "Abliterated" removes the *model's own* refusal
> behavior. It does **not** weaken Sonder's guardrails: permission rules,
> workspace containment, tool policy, and consent gates are enforced by
> the host, not the model. An uncensored model driving Sonder still cannot
> write outside authorized roots, delete without confirmation, or reach
> the network with web tools off. See
> [security-model](../wiki/09-security-model.md).

## 1. Prerequisites

- Ollama installed and running (`ollama serve` or the tray app).
- The Sonder repo checked out with its venv (see
  [install-workstation-local](install-workstation-local.md)).
- The USB mounted, or the `.gguf` copied to disk. RAM: 8 GB minimum,
  16 GB recommended for a 4B q4 model.

## 2. Import the model from USB

Let Sonder find and import the GGUF off the mounted stick:

```bash
./venv/bin/python setup_alias.py --from-usb
```

This scans the usual mount points (`/media/*`, `/run/media/*`, `/mnt/*`,
`/Volumes/*`), imports the single `.gguf` it finds as the `sonder:latest`
alias via an Ollama `FROM <path>` Modelfile (Ollama copies the weights
into its own store, so the USB can be removed afterward), and pulls the
embedding model if it is reachable.

If several models are present, pin one:

```bash
./venv/bin/python setup_alias.py --gguf "/Volumes/FACTS/qwen3.5-4b.gguf"
```

Add extra scan locations with `--usb-root` (repeatable). To import a file
already on disk, pass its path to `--gguf` directly.

## 3. Verify the import

```bash
ollama list                       # sonder:latest should appear
ollama run sonder:latest "Say hello in one short sentence."
```

## 4. Wire the tiers

The import aliases the model as `sonder:latest`, which the `code`/`general`
tiers use by default. To also make it the fast/router tier, or to keep a
separate coder on the heavy lanes, set the tier environment variables:

```bash
export SONDER_FAST=sonder:latest        # router / titling / summaries
export SONDER_GENERAL=sonder:latest     # general chat
# Optional split: keep a coder model on the workbench/code lane
# export SONDER_CODE=qwen2.5-coder:7b
```

Or set the routing lanes in the hot-reloadable runtime policy so different
lanes use different models (see
[model-tiers-and-gateway](../wiki/08-model-tiers-and-gateway.md)).

## 5. Validate through Sonder

```bash
./venv/bin/python -m sonder_runtime preflight --skip-ollama   # config/schema
ollama ps                                                     # model resident?
./venv/bin/python -m sonder_runtime serve                    # or: repl
```

Then chat through the API/REPL. Confirm memory and tools work:

- ask something, then in a later turn confirm it recalled context;
- try `/run 15` on a generated code block to confirm guarded execution;
- run `/permissions` and `/filepolicy` to confirm the guardrails are active.

## Two sticks / multiple models

Two copies of the same 4B do not speed up a single request, but Sonder's
fleet/autopilot and `parallel_run_code` run multiple model workers
concurrently, so two instances roughly double throughput on parallel
work. You can also split by lane — 4B on `router`/`general`, a coder on
`workbench`/`review`. See
[agent-autopilot-fleet](../wiki/07-agent-autopilot-fleet.md).

## Reverting

The imported model lives in Ollama's store; remove it with
`ollama rm sonder:latest`, then re-run `setup_alias.py` (online) to
restore the default coder alias.
