# Sonder Runtime

<p align="center">
  <img src="docs/assets/brand/sonder-runtime-badge.png" alt="Sonder Runtime" width="720">
</p>

<p align="center"><strong>Your models. Your memory. Your machine.</strong></p>

Sonder is a private, adaptive AI runtime that brings models, memory, tools,
agents, and optional training into one local-first system. It runs around an
Ollama model rather than shipping model weights itself, so you can choose the
model and hardware that fit your needs.

<!-- ci-artifact-badges:start -->
[![Prerelease downloads](https://img.shields.io/badge/app--latest-prerelease-2088FF?style=for-the-badge&logo=github&logoColor=white)](https://github.com/Krilliac/Sonder-runtime/releases/tag/app-latest)
[![Android prerelease](https://img.shields.io/badge/Android-prerelease-3DDC84?style=for-the-badge&logo=android&logoColor=white)](https://github.com/Krilliac/Sonder-runtime/releases/download/app-latest/sonder-runtime-android.apk)
[![Linux prerelease](https://img.shields.io/badge/Linux-prerelease-FCC624?style=for-the-badge&logo=linux&logoColor=black)](https://github.com/Krilliac/Sonder-runtime/releases/download/app-latest/sonder-runtime-linux-x64.tar.gz)
[![Windows prerelease](https://img.shields.io/badge/Windows-prerelease-0078D4?style=for-the-badge&logo=windows&logoColor=white)](https://github.com/Krilliac/Sonder-runtime/releases/download/app-latest/sonder-runtime-windows-x64.zip)
[![macOS prerelease](https://img.shields.io/badge/macOS-prerelease-000000?style=for-the-badge&logo=apple&logoColor=white)](https://github.com/Krilliac/Sonder-runtime/releases/download/app-latest/sonder-runtime-macos.zip)
<!-- ci-artifact-badges:end -->

## Why Sonder

- **Private by default.** Local models, embeddings, memory, and tools stay on
  your machine. Web, remote Ollama, and cloud tiers require separate opt-ins.
- **Learns from real outcomes.** Sonder stores what worked after a compile,
  test, or user judgment, then retrieves relevant lessons for later work.
- **Useful beyond chat.** Guarded file tools, code execution, repository
  inspection, research, artifacts, and bounded agent fleets share one runtime.
- **Model and hardware agnostic.** Use the Ollama model that fits your CPU,
  NVIDIA/AMD/Intel GPU, Apple unified memory, or mixed-device setup.
- **Available where you work.** Use the desktop/mobile app, terminal REPL,
  OpenAI-compatible API, MCP tools, or a private remote server.
- **Inspectable and recoverable.** Activity evidence, policy gates, backups,
  transactional operations, and rollback paths are built into the workflow.

## How it fits together

```text
App / terminal / API / MCP
            |
            v
Sonder Runtime
policy + memory + tools + agents + training control
            |
            v
Ollama
model storage + CPU/GPU inference
```

- **Retrieval** — hybrid lexical (SQLite FTS5) + semantic (local embeddings), with a **relevance threshold** so only genuinely on-topic lessons are injected (irrelevant lessons hurt, so it injects nothing when nothing fits).
- **Capture** — every learning call is logged locally to `memory.db`.
- **Grounding** — you (or your fleet) call `record_outcome` with a real signal; execution outcomes are the reward.
- **Reflection** — a good outcome distills a deduped one-line lesson future prompts can retrieve.

The loop is model-agnostic: point it at a compatible Ollama model. The selected
local `code` model is memory-augmented; a stronger paid/cloud model can answer
without local-lesson injection while its grounded good outcomes are distilled into
lessons and fine-tuning data the local model retrieves later. Which
tiers learn is configurable (`SONDER_LEARN_TIERS`, default local aliases:
`fast,code,general`). Memory, capture, and distillation stay on the runtime host.
A cloud-tier prompt leaves only after `SONDER_ALLOW_CLOUD=1`. The `fast`, `code`,
and `general` aliases use loopback Ollama by default; pointing `OLLAMA_HOST` at a
non-loopback server is blocked unless `SONDER_ALLOW_REMOTE_OLLAMA=1` explicitly
acknowledges that prompts and embeddings leave this machine. Remote Ollama and
hosted/cloud consent are separate gates. Their shared mappings
and each execution lane's preferred alias live in the hot-reloadable runtime
policy described below. Environment values seed the first policy file; cloud
aliases and cloud opt-in remain separate host-owned configuration.

Loopback model requests use one bounded, same-model retry by default for narrowly
transient transport failures and HTTP 408/429/502/503/504 responses. The retry
shares the original timeout budget, checks fleet cancellation before resending,
and never changes endpoint, model, or tier. Hosted/cloud calls are always
single-attempt to avoid silently duplicating metered work. Set
`SONDER_LOCAL_RETRIES=0..2` and `SONDER_LOCAL_RETRY_DELAY_MS=0..1000` to tune the
loopback policy. Explicit remote Ollama and hosted/cloud calls are single-attempt.

One extra attempt exists on top of that policy, and only for a *classified*
context overflow. The gateway reads the failure text - never the HTTP status,
which providers and proxies get wrong often enough that a real overflow can
arrive as a 429 - and if it says the prompt did not fit, a loopback call may
compact the prompt once and resend it inside the same timeout and cancellation
budget. Compaction drops the oldest turns and leaves a short in-band note, the
same discipline the session live-turn window already uses; it never rewrites or
truncates a message, and it never raises `num_ctx` behind the context policy's
back. A request that is one oversized turn has nothing safe to drop and is
reported rather than retried. Body-too-large, device out-of-memory, plain rate
limits, and missing models are recognised as explicitly *not* overflow and are
never retried this way. Hosted and remote routes keep their single-attempt
posture unless the call site declares the request idempotent **and**
`SONDER_HOSTED_OVERFLOW_RETRY=1` is set.
Good-outcome lesson reflection does not queue another model request behind an
active fleet: the outcome is committed immediately and its lesson remains
retryable. When the fleet is idle, reflection uses a separate shared generation
and embedding budget (`SONDER_DISTILLATION_TIMEOUT`, default `20` seconds,
bounded by `SONDER_TIMEOUT`) so `record_outcome` cannot inherit the normal
five-minute model-call ceiling.

Sonder is a runtime, not a foundation model. Names such as
`sonder:latest` are local Ollama aliases; the underlying weights remain managed
by Ollama. Ollama is the local model server.

## Model requirements

You do **not** need every model or every tier in the catalog.

| What you want to use | What must be available locally |
|---|---|
| Normal REPL, API chat, and code work | Ollama plus one generative local model. Bootstrap or `setup_alias.py` chooses/pulls that base model and exposes it as `sonder:latest`. The `fast`, `code`, and `general` roles can all share it. |
| Semantic memory, lesson recall, and vector search | An embedding model as well (the bootstrap default is `nomic-embed-text`). Core chat still starts without one, but those semantic features are unavailable until an embedding model is configured. |
| Image or screenshot analysis | An explicitly configured local vision-language model; it is only needed when using a vision-capable feature. |
| Separate reasoning, reranking, extraction, tool-oriented, speech, or experimental model families | Optional specialist models. Install and bind them only when the matching feature is enabled and supported. |
| Cloud tiers | No local download, but explicit cloud opt-in is required and prompts leave the machine for those calls. |

The bootstrap and `setup_alias.py` intentionally **try** to pull both a base
model and the default embedding model so a new local installation has memory
enabled out of the box. The generative base model is required; if the optional
embedding pull is unavailable, bootstrap still creates `sonder:latest` and
explains how to enable recall/lessons later. See the
[Model Catalog](docs/wiki/18-model-catalog.md) for tier bindings and the
[collection runbook](docs/runbooks/assemble-model-collection.md) for specialist
setups. Use `setup_alias.py --no-embedding` when you intentionally want the
smallest chat-only installation.

> Installing a tag only puts a model in Ollama's catalog; it does not by itself
> enable a Sonder feature. Bind supported models through `/runtime set ...` and
> use the matching tool or route. A downloaded speech or reranker tag remains
> optional until Sonder has a provider-backed integration for that capability.

### Verify and choose your models

All read-only. Run these after bootstrap, or any time a model-related error
looks like configuration rather than capability.

```bash
ollama list                          # what the provider actually has
ollama show sonder:latest            # prove the stable alias exists
python -m sonder_runtime doctor      # config, runtime policy, Ollama reachability
python -m sonder_runtime preflight   # startup checks without binding a port
```

In the REPL, `/model` lists every selectable tier plus the installed tags and
switches this session (`/model general`, `/model qwen2.5-coder:7b`); `/runtime`
shows the shared tier→model and lane→tier policy with a readiness summary, and
`/runtime set code=<installed-model>` rebinds it for every surface.

Missing models degrade rather than crash: an unbound `reasoning`/`vision` tier
is simply not offered and the router falls back to a base tier, a missing
embedding model disables semantic recall while chat and lexical retrieval keep
working, and a `cloud-*` tier is withheld entirely until you opt in. If
`sonder:latest` itself is gone, re-run `setup_alias.py`. Full behavior table,
per-surface commands, and the local-vs-cloud gates:
[Model Requirements & Onboarding](docs/wiki/19-model-requirements-and-onboarding.md).

## Quick start

The `app-latest` badges are a mutable prerelease snapshot. They may lag `main`
and are not a versioned, release-ready build. Versioned `app-vX.Y.Z` releases
must pass the repository's version, artifact-integrity, SBOM, and provenance
gates.

### Packaged app or bundled runtime

Run the one-time bootstrap from the extracted bundle. It starts Ollama and
chooses a compatible local model. A bundle is intentionally self-contained:
it does **not** modify your shell `PATH`. Use its launcher directly, or add
the bundle directory to `PATH` yourself if you want to invoke `sonder` by
name in later terminals.

```powershell
# Windows PowerShell, from the installed bundle
.\bootstrap-engine.cmd
.\sonder.cmd
```

```bash
# Linux/macOS, from the installed bundle
./bootstrap-engine.sh
. ./sonder-runtime.sh
"$SONDER_PYTHON" -m sonder_runtime repl
```

### From source

Install and start [Ollama](https://ollama.com) first. The following command
blocks create a virtual environment, install the runtime, configure the local
`sonder` model alias, and open the REPL.

```bash
# Linux/macOS
git clone https://github.com/Krilliac/Sonder-runtime.git
cd Sonder-runtime
python3 -m venv venv
./venv/bin/pip install -r requirements-runtime.txt
./venv/bin/python setup_alias.py
./venv/bin/python -m sonder_runtime repl
```

```powershell
# Windows PowerShell
git clone https://github.com/Krilliac/Sonder-runtime.git
Set-Location Sonder-runtime
python -m venv venv
.\venv\Scripts\pip.exe install -r requirements-runtime.txt
.\venv\Scripts\python.exe setup_alias.py
.\venv\Scripts\python.exe -m sonder_runtime repl
```

`packaging\install_workstation_local.ps1` automates the venv/install/preflight
steps above in one idempotent command; see
[install-workstation-local](docs/runbooks/install-workstation-local.md).

For a Linux loopback service install that provisions Ollama, its local model,
and systemd in one step, run `bash deploy_sonder.sh --serve` as root from a
source checkout. Use the production installer and TLS runbook for non-loopback
hosting.

### Launch and use

After an installation has added a `sonder` launcher to your `PATH`, simply
type `sonder` in Bash or PowerShell. (On Windows, `sonder` resolves to
`sonder.cmd`; an extracted bundle alone is not a `PATH` installation.) From a
source checkout or an extracted POSIX bundle, use the explicit command below.
In either case, ask normally; type `/help` for guarded commands and use the
visible composer shortcuts for history and editing.

```bash
sonder
# or, from this source checkout:
python -m sonder_runtime repl
# sonder > explain this repository's test layout
```

<p align="center">
  <img src="docs/assets/repl/terminal-repl.png" alt="Sonder terminal REPL with a framed dark-blue composer, live context and token statistics, and activity summary" width="1000">
</p>

The terminal REPL keeps the current model, active lanes, context budget, token
usage, and elapsed time visible while you work. Type normally, use `/help` to
discover guarded commands, and use `/model <tag-or-tier>` to choose an
installed chat model. `/sessions` lists past threads with age and project,
`/replay [id|title] [N]` re-renders a stored thread read-only, and
`/resume <id|title>` continues one. Known failures add a one-line `hint:`
under the interactive error panel; piped output stays plain and script-safe,
and `SONDER_REPL_NDJSON=1` opts a piped session into one JSON line per turn
(schema `sonder.repl-turn.v1`).

### Check or update this source checkout

The REPL banner shows its loaded and newest known source revisions without
network I/O. Use `/updatecheck` to refresh the canonical `origin/main` ref and
report the installed/newest commit and timestamps. `/update` is a guarded
fast-forward for a clean checkout on `main` with the canonical Sonder remote;
it refuses feature branches, local commits, and dirty trees. Restart Sonder
after a successful source update. These REPL commands are separate from the
signed release-manager commands documented in the update guide.

If local workflow edits make a source checkout dirty, `/stash` shows the
recovery state; `/stash save` preserves tracked edits, `/stash save-untracked`
also preserves generated files, and `/stash pop` restores the most recent
recovery stash only onto a clean canonical `main` checkout. Saved workflows
now live in the normal per-user state directory (for example,
`%LOCALAPPDATA%\sonder\workflows.json` on Windows), so new workflow saves no
longer dirty an installed source tree. Existing root `workflows.json` files are
copied once on first use; the original is left untouched for review.

Start the loopback OpenAI-compatible API in a second terminal when another
client needs it. Run `doctor` first if this is a new machine.

```bash
python -m sonder_runtime doctor
python -m sonder_runtime serve
curl http://127.0.0.1:11435/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"sonder","messages":[{"role":"user","content":"Hello"}]}'
```

```powershell
# PowerShell equivalent (run after `python -m sonder_runtime serve`)
$body = @{
  model = "sonder"
  messages = @(@{ role = "user"; content = "Hello" })
} | ConvertTo-Json -Depth 4
Invoke-RestMethod http://127.0.0.1:11435/v1/chat/completions `
  -Method Post -ContentType "application/json" -Body $body
```

For a source checkout, prefix the commands with the venv Python shown above;
for example `./venv/bin/python -m sonder_runtime serve` or
`.\venv\Scripts\python.exe -m sonder_runtime serve`. For an MCP client, run
`python -m sonder_runtime mcp`. See the
[getting-started guide](docs/wiki/02-getting-started.md),
[HTTP API reference](docs/wiki/05-http-api-and-lifecycle.md), and
[CLI reference](docs/wiki/04-cli-and-entrypoint.md) for configuration,
authentication, hosting, and full command details.

## What you get

| Area | Highlights |
|---|---|
| Models | Local fast/code/general tiers, explicit cloud fallbacks, model routing |
| Memory | Hybrid retrieval, project scoping, grounded lessons, preferences |
| Tools | Guarded files, repositories, code, data, web, artifacts, office formats |
| Agents | Single agent, durable autopilot, bounded parallel fleets, cancellation |
| Apps | Windows, Linux, macOS, Android, terminal, API, and MCP clients |
| Operations | Activity evidence, backups, recovery, signed updates, live reload |
| Personalization | Optional adapter training with validation, deployment, and rollback |

## Honest boundaries

- A small local model is not a frontier model. Give it the facts, delegate
  bounded transformations, and review its work.
- Learning is grounded only when a caller records a real outcome; self-graded
  success is not treated as proof.
- Remote access is powerful because Sonder can execute code and modify files.
  Never expose the convenience loopback service directly to a network.
- Multi-PC inference is supported with `SONDER_OLLAMA_WORKERS`: each host runs
  its own Ollama/model store and Sonder schedules requests across HTTPS worker
  origins with bounded transport failover. This is request-level pooling, not
  model-weight sharding or shared-memory GPU federation.
- Deliberately unrestricted model testing is available only through the
  exact-acknowledgement [unsafe lab runbook](docs/runbooks/unsafe-lab.md). It
  removes model-loop host-tool policy; it does not provide OS isolation.
- NPU support is an optional utility path for compatible routing or embedding
  work; token generation remains on the model server's CPU/GPU path.

## Documentation

- [Getting started](docs/wiki/02-getting-started.md)
- [Architecture](ARCHITECTURE.md)
- [Configuration](docs/wiki/03-configuration.md)
- [Agent, autopilot, and fleets](docs/wiki/07-agent-autopilot-fleet.md)
- [Models and gateways](docs/wiki/08-model-tiers-and-gateway.md)
- [Model requirements and onboarding](docs/wiki/19-model-requirements-and-onboarding.md)
- [Tools and languages](docs/wiki/10-tools-and-languages.md)
- [Natural-language tool calling](docs/NATURAL_LANGUAGE_TOOLS.md)
- [Natural-language capability queries](docs/NATURAL_LANGUAGE_CAPABILITY_QUERIES.md)
- [Training](TRAINING.md)
- [Client and private hosting](CLIENT.md)
- [Developer SDK contracts and plugin manifests](docs/developer-sdk.md)
- [Unsafe lab model testing](docs/runbooks/unsafe-lab.md)
- [Runbooks](docs/runbooks/README.md)
- [Full wiki](docs/wiki/README.md)

## Security and contributing

Read [SECURITY.md](SECURITY.md) before enabling remote access. Use the
server-private profile behind TLS and report vulnerabilities privately through
GitHub's Security tab.

Optional direct MCP container execution is documented in
[docs/security/ISOLATED_EXECUTION.md](docs/security/ISOLATED_EXECUTION.md). It
uses a fixed Docker/Podman policy and is stronger than local `run_code`, but it
still relies on the external runtime and host kernel and is not escape-proof.

Contributions are welcome under the [Apache License 2.0](LICENSE). See
[CONTRIBUTING.md](CONTRIBUTING.md) for the development and review workflow.
- WP1 Forty-Fifth Slice: the HTTP chat usage presentation helper now lives in `sonder_runtime.adapters.observability.chat_formatting`.
- WP1 Forty-Sixth Slice: the pure REPL duration presentation helper now lives in `sonder_runtime.adapters.observability.repl_formatting`.
- WP1 Forty-Seventh Slice: the active architecture legacy-root policy now covers only roots with live package callers; `autopilot_store` is migration-only.
- WP1 Forty-Eighth Slice: pure HTTP command-completion limit normalization now lives in `sonder_runtime.adapters.command_completion`.
- WP1 Fifty-First Slice: pure runtime model-readiness presentation now lives in `sonder_runtime.adapters.runtime_readiness_formatting`.
- WP1 Fifty-Second Slice: pure run-result presentation now lives in `sonder_runtime.adapters.observability.run_result_formatting`.
- WP1 Fifty-Fifth Slice: the Ollama gateway now reads its endpoint through the packaged `sonder_runtime.adapters.ollama.endpoint` boundary; the remaining server model-routing dependency is explicit.
- WP1 Fifty-Fourth Slice: pure goal presentation now lives in `sonder_runtime.adapters.goal_formatting`.
- WP1 Fifty-Sixth Slice: pure learning-tier model/provider presentation now lives in `sonder_runtime.adapters.learning_tier_formatting`.
- WP1 Sixty-Seventh Slice: the embedding-cache adapter now consumes database paths through `sonder_runtime.platform.paths`.
- WP1 Fifty-Seventh Slice: the import-time model/tier seed now has a typed immutable projection in `sonder_runtime.domain.runtime_model_configuration`; server compatibility aliases and live policy refresh remain intact.
- WP1 Fifty-Eighth Slice: Ollama process-lifecycle policy now lives in the packaged `sonder_runtime.adapters.ollama_lifecycle` boundary; the root module remains a compatibility import.
- WP1 Fifty-Ninth Slice: the live cloud-default compatibility repair now consumes its replacement from the frozen typed runtime-model configuration projection.
- WP1 Sixtieth Slice: the root Ollama lifecycle compatibility import now exposes only the packaged adapter's two public helpers; private process and trust hooks remain package-internal.
- WP1 Seventy-Fifth Slice: the filesystem workbench now consumes the canonical `sonder_runtime.platform.logging` seam, preserving handler setup and redaction semantics.
- WP1 Eighty-First Slice: filesystem operations now consume the canonical `sonder_runtime.platform.paths` seam, preserving default-home resolution and containment semantics.
- WP1 Seventy-Seventh Slice: local observability now consumes the canonical `sonder_runtime.platform.logging` seam, preserving logger identity and redaction semantics.
  - WP1 Seventy-First Slice: the packaged preference adapter now resolves its default memory database path through `sonder_runtime.platform.paths`.
- WP1 Sixty-First Slice: the preflight adapter now consumes `SonderConfig` through `sonder_runtime.platform.config`, preserving the root implementation's environment/default semantics while reducing a package caller's root dependency.
- WP1 Sixty-Fourth Slice: the HTTP interface now reads the API-key policy through `sonder_runtime.platform.config`, preserving the canonical configuration defaults and environment-backed implementation.
- WP1 Sixty-Third Slice: the packaged entrypoint now consumes typed configuration through `sonder_runtime.platform.config`, preserving the historical `sonder_paths` compatibility attribute.
- WP1 Sixty-Sixth Slice: the evaluation-history adapter now consumes state locations through `sonder_runtime.platform.paths`, preserving existing path resolution and migration behavior.
- WP1 Sixty-Eighth Slice: local-system packaging now proves that retired `eval_history.py` is excluded while the canonical evaluation-history store and application package are included.
- WP1 Sixty-Ninth Slice: the runtime-policy adapter now consumes its state-file path through `sonder_runtime.platform.paths`, preserving explicit policy-path overrides and legacy home resolution.
- WP1 Seventieth Slice: the packaged HTTP lifecycle now consumes the shutdown coordinator through `sonder_runtime.platform.shutdown`, preserving the root implementation and all drain semantics.
- WP1 Seventy-Second Slice: the packaged HTTP lifecycle now consumes `MetricsRegistry` through `sonder_runtime.platform.metrics`, preserving metric names, labels, and semantics.
- WP1 Seventy-Fourth Slice: the packaged backup adapter now consumes build identity through `sonder_runtime.platform.version`, preserving stamped version and commit metadata.
- WP1 Sixty-Fifth Slice: the read-only doctor checks now load typed configuration through `sonder_runtime.platform.config`, preserving diagnostic output and error handling.
- WP1 Fiftieth Slice: the active architecture legacy-root policy now excludes `fleet_store`; its root alias remains only for immutable migration replay.
WP1 Seventy-Eighth Slice: the packaged NPU manifest adapter now resolves its manifest directory through the canonical platform path seam.
WP1 Seventy-Ninth Slice: the NPU service now resolves its shadow-ledger state file through the canonical platform path seam.

WP1 Eighty-Eighth Slice: the operations store now consumes redaction through the canonical `sonder_runtime.platform.logging` seam, preserving durable event persistence semantics.
WP1 Ninetieth Slice: the strangler unit-of-work now resolves its default memory database through `sonder_runtime.platform.paths`, preserving the live memory-store port and explicit path overrides.
WP1 Ninety-First Slice: the filesystem workbench now resolves its Bash executable through `sonder_runtime.platform.paths`, preserving workspace resolution and containment semantics.
WP1 Eighty-Ninth Slice: the migrations adapter now resolves the operations database through the canonical `sonder_runtime.platform.paths` seam; immutable migration replay and database locations are unchanged.
WP1 Eightieth Slice: the packaged secret-rotation adapter now resolves its default rotation state through the canonical platform path seam.
WP1 Eighty-Second Slice: the update engine now reads build identity through the canonical platform version seam; signed-update verification and release metadata behavior are unchanged.
WP1 Eighty-Third Slice: workflow persistence now resolves its mutable state home through the canonical platform path seam; workspace overrides, containment, legacy migration, and atomic writes are unchanged.
WP1 Eighty-Fourth Slice: the update service now reads build identity through the canonical platform version seam; bundle metadata and signed-update verification behavior are unchanged.
WP1 Eighty-Sixth Slice: fleet persistence now resolves its database and principal-credential paths through the canonical `sonder_runtime.platform.paths` seam; SQLite and migration semantics are unchanged.
- WP1 Eighty-Seventh Slice: queued-action persistence now resolves its database path through the canonical `sonder_runtime.platform.paths` seam; queue and immutable migration semantics are unchanged.
- WP1 Ninety-Second Slice: the update engine now resolves default release and active-pointer paths through the canonical `sonder_runtime.platform.paths` seam; signed-update verification and bootstrap behavior are unchanged.
- WP1 Ninety-Fourth Slice: the migrations adapter now resolves all store paths and the migration lock through the identity-preserving `sonder_runtime.platform.paths` seam; immutable migration replay and database locations are unchanged.
- WP1 Ninety-Fifth Slice: the packaged entrypoint now resolves its default backup target through `sonder_runtime.platform.paths`, preserving default-home resolution and explicit/configured target precedence.
- WP1 Eighty-Fifth Slice: moved the autopilot persistence database-path caller to `sonder_runtime.platform.paths` while preserving database and migration semantics.
- WP1 Ninety-Third Slice: packaged entrypoint build metadata now crosses the canonical platform version boundary; root release metadata remains available for tooling compatibility.
- WP1 Ninety-Sixth Slice: packaged web lifecycle build identity now crosses the canonical platform version boundary; lifecycle metrics and version payloads remain unchanged.
- WP1 Ninety-Seventh Slice: filesystem path implementation ownership now lives in `sonder_runtime.platform.paths`; the root `sonder_paths` module remains a thin identity-preserving compatibility alias with environment and legacy-migration behavior unchanged.
- WP1 Ninety-Eighth Slice: structured logging, redaction, and child-environment filtering now belong to `sonder_runtime.platform.logging`; `sonder_logging` remains a thin module-identity compatibility shim preserving logger and monkeypatch behavior.
- WP1 One-Hundred-Eleventh Slice: metrics ownership is now single-path under `sonder_runtime.platform.metrics`; the duplicate `sonder_metrics.py` root delegate is retired after all production callers and focused tests moved to the canonical module.
- WP1 One-Hundred-Twelfth Slice: unsafe-lab state now belongs to the security adapter, with pure explicit-input policy separated into the platform seam and zero architecture violations.
- WP1 One-Hundred-Twenty-Third Slice: durable operations-event sink ownership now belongs to `sonder_runtime.adapters.operations_event_sink`; the generic strangler name remains an identity-preserving compatibility alias.
- WP1 One-Hundred-Twenty-Fourth Slice: pure schema-gap formatting now belongs to `sonder_runtime.domain.schema_policy`, preserving the server compatibility alias.
- WP1 One-Hundred-Thirteenth Slice: autopilot repository ownership now belongs to `sonder_runtime.adapters.persistence.autopilot_repository`, removing the generic strangler repository implementation.
- WP1 One-Hundred-Fourteenth Slice: HTTP serve-temperature policy now belongs to `sonder_runtime.interfaces.http.serve_policy`, preserving the server compatibility alias.
- WP1 One-Hundred-Sixteenth Slice: process-probe ownership now belongs to `sonder_runtime.adapters.process_probe.ProcessProbeAdapter`; the generic strangler no longer owns that port adapter.
- WP1 Two-Hundred-Twenty-Fifth Slice: live-process fingerprint selection now belongs to `sonder_runtime.adapters.process_liveness.process_identity`; `ProcessProbeAdapter.identity` remains the compatibility port surface.
- WP1 One-Hundred-Fifteenth Slice: pure model-catalog capability normalization now belongs to `sonder_runtime.domain.model_capabilities`, preserving the server compatibility alias.
- WP1 One-Hundred-Eighteenth Slice: pure inline-thinking output policy now belongs to `sonder_runtime.domain.thinking_policy`, preserving the server compatibility alias.
- WP1 One-Hundredth Slice: full shutdown coordination now belongs to `sonder_runtime.platform.shutdown`; `sonder_shutdown` remains an identity-preserving compatibility shim with cancellation, signal, drain, deadline, and concurrent idempotence semantics unchanged.
- WP1 One-Hundred-Fifth Slice: process and dependency state now belong to `sonder_runtime.platform.service_state`; `sonder_service_state` remains an identity-preserving compatibility shim while lifecycle and shutdown consume the canonical implementation.
- WP1 One-Hundred-First Slice: build identity implementation now belongs to `sonder_runtime.platform.version`; `sonder_version.py` retains its literal release-tooling `VERSION` and identity-preserving compatibility surface.
- WP1 One-Hundred-Third Slice: the persistence migration registry now consumes build identity from `sonder_runtime.platform.version`; immutable migration bytes, checksums, replay, and release metadata remain unchanged.
- WP1 One-Hundred-Sixth Slice: full system-profile implementation ownership now lives in `sonder_runtime.platform.system_profile`; the root module remains an identity-preserving shim while hardware detection, mutable probe state, profile editing, and monkeypatch behavior remain unchanged.
- WP1 One-Hundred-Seventh Slice: the `sonder_version` root-platform allowance is removed after all packaged runtime callers moved to `sonder_runtime.platform.version`; the literal root `VERSION` contract remains intact for release tooling.
- WP1 One-Hundred-Eighth Slice: pure Ollama-origin normalization and fail-closed security policy now live in `sonder_runtime.domain.ollama_policy`; `unsafe_lab` no longer imports the transport adapter, preserving the security gate while removing the blocked platform-to-adapter dependency.
- WP1 Two-Hundred-Twenty-Sixth Slice: system-profile boolean environment overrides now use the canonical `sonder_runtime.platform.config_environment` policy, preserving the `_env_bool` compatibility alias and hardware override behavior.
- WP1 Two-Hundred-Twenty-Eighth Slice: launcher-health nonce, identity, and HMAC proof status policy now live in the packaged `sonder_runtime.domain.launcher_health` boundary, preserving the root `sonder_health` compatibility aliases.
- WP1 Two-Hundred-Forty-Sixth Slice: the remaining root doctor configuration-check policy now lives in the packaged `sonder_runtime.bootstrap.config_loading` boundary, preserving the root `_check_config` compatibility delegate.
- WP1 Two-Hundred-Thirtieth Slice: the bounded local-observability percentile helper now lives in `sonder_runtime.adapters.observability.latency_formatting`, preserving the `local_observability._percentile` compatibility alias and the root logging identity.
- WP1 Two-Hundred-Thirty-First Slice: headless argument parsing and command sequencing now live in `sonder_runtime.interfaces.cli.headless`, while `sonder_headless.py` preserves the supervisor implementation and compatibility surface.
- WP1 Two-Hundred-Thirty-Third Slice: unstamped build identity now reuses the packaged version commit probe, preserving the root `sonder_version` module identity and `_commit_from_git` compatibility helper.
- WP1 Two-Hundred-Thirty-Fourth Slice: cooperative cancellation now belongs to `sonder_runtime.platform.process`; packaged shutdown and root `sonder_shutdown` keep identity-preserving aliases.
- WP1 Two-Hundred-Thirty-Fifth Slice: speculative-tool safety policy now lives in the packaged `sonder_runtime.domain.speculation_policy` boundary, preserving the root `sonder_speculation.SPECULATABLE_TOOLS` alias and predictor seam.
- WP1 Two-Hundred-Forty-Third Slice: speculative-execution configuration helpers now live in the packaged `sonder_runtime.platform.speculation` boundary, preserving root helper aliases and the packaged domain safety policy.
- WP1 Two-Hundred-Thirty-Sixth Slice: the debug-dump export boundary now imports `Redactor` from canonical packaged logging, preserving the `debug_dump.Redactor` and root `sonder_logging` identities.
- WP1 Two-Hundred-Thirty-Seventh Slice: the lifecycle metric projection now belongs to `sonder_runtime.application.lifecycle`, preserving the web `_state_number` alias and root `sonder_service_state` identity.
- WP1 Two-Hundred-Forty-Second Slice: the remaining pure hardware sizing
  helpers now belong to the packaged domain boundary; accelerator and host
  platform probe ownership is documented without changing inventory or
  filesystem-text behavior.
- WP1 Two-Hundred-Forty-Fifth Slice: root hardware probe classification now
  uses identity-preserving aliases to the packaged platform boundary; the
  accelerator and host-platform probe seams remain explicitly packaged.
- WP1 Two-Hundred-Forty-Eighth Slice: standalone-client endpoint comparison and connection-error fallback now live in packaged client adapters; root names remain compatibility aliases.
- WP1 Two-Hundred-Forty-Ninth Slice: pure launcher output-tail, timeout, and operation-retention policy now live in `sonder_runtime.adapters.launcher_output`; root private helper names remain identity-preserving compatibility aliases.
- WP1 Two-Hundred-Fiftieth Slice: read-only memory-quality doctor policy now lives in `sonder_runtime.bootstrap.doctor_checks`, preserving the root `_check_memory_quality` compatibility delegate and injected legacy collaborators.
- WP1 Two-Hundred-Thirty-Eighth Slice: context-health text formatting now belongs to the packaged observability health-formatting boundary, preserving the generic packaged formatter alias while leaving the root launcher-health contract unchanged.
- WP1 Two-Hundred-Thirty-Ninth Slice: standalone-client HTTP execution now lives in `sonder_runtime.adapters.client_transport`, preserving the root `send_prompt` and `build_request` compatibility seams.
- WP1 Two-Hundred-Fortieth Slice: pure doctor terminal formatting and status rollup now live in `sonder_runtime.bootstrap.doctor_formatting`, preserving root rendering, status, and rollup aliases.
- WP1 Two-Hundred-Forty-First Slice: pure thinking-budget exhaustion detection now lives in `sonder_runtime.domain.thinking_policy`, preserving the root `server._thinking_exhausted_budget` alias.
- WP1 Two-Hundred-Forty-Seventh Slice: pure agent tool-invocation mutation policy now lives in `sonder_runtime.domain.agent_mutation_policy`, preserving the root mutation tool-set and predicate aliases.
- WP1 Two-Hundred-Forty-Fourth Slice: launcher idempotency-key normalization and durable replay validation now live in `sonder_runtime.adapters.launcher_idempotency`, preserving root helper and regex aliases.
- WP1 Two-Hundred-Twenty-Seventh Slice: environment-file parsing now belongs to the packaged `sonder_runtime.platform.config_environment` policy boundary, preserving the root `sonder_config.parse_env_file` and `ConfigError` contract.
- WP1 Two-Hundred-Twenty-Ninth Slice: packaged HTTP default-home and server-log resolution now use the canonical `sonder_runtime.platform.paths` boundary; the root `sonder_paths` identity alias remains compatible.
- WP1 Two-Hundred-Thirty-Second Slice: pure launcher lifecycle `context_size` normalization now belongs to `sonder_runtime.application.lifecycle`, preserving the root `sonder_launcher` helper and compatibility constants.
- WP1 One-Hundred-Second Slice: complete typed configuration ownership now belongs to `sonder_runtime.platform.config`; `sonder_config` remains a thin external-tooling compatibility surface with exact class, loader, exception, precedence, default, and validation semantics preserved.
# WP1 One-Hundred-Tenth Slice

- Extracted the state-independent runtime identity prompt renderer into
  `sonder_runtime.domain.runtime_identity`, reducing composition-root policy
  while preserving the `server._runtime_identity_block` compatibility surface.
- WP1 Two-Hundred-Ninety-Sixth Slice: pure fanout prompt-echo redaction now lives in `sonder_runtime.domain.fanout_redaction`, preserving the root `_fanout_redact_prompt_echo` alias.
- WP1 Two-Hundred-Ninety-Seventh Slice: pure agent decision parsing now lives in `sonder_runtime.domain.agents.decision_parsing`, preserving the root `_extract_agent_json` alias.
- WP1 Two-Hundred-Ninety-Eighth Slice: pure improvement report rendering now lives in `sonder_runtime.domain.improvement_report_formatting`, preserving the root `format_improvement_report` alias.
- WP1 Two-Hundred-Ninety-Ninth Slice: the pure natural-language model and fanout request grammar now lives in `sonder_runtime.domain.natural_model_request`, preserving the root `natural_model_request` and `_fanout_profile_scope` delegates and the selector constant aliases.
