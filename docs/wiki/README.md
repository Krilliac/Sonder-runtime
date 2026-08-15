# Sonder Runtime — Wiki

In-depth documentation for the whole system: what each subsystem is, how
it works, how to set it up, and how the pieces fit. If you are new, read
[Architecture](01-architecture.md) then [Getting Started](02-getting-started.md).

Sonder is a **private, model-agnostic orchestration runtime** around
[Ollama](https://ollama.com). Ollama stores and runs the model weights;
Sonder supplies routing, prompts, memory, retrieval, guarded tools,
activity evidence, policy, autopilot/fleet automation, training control,
backups, and a signed update path. It is not a foundation model.

## Map

| # | Page | What it covers |
|---|---|---|
| 01 | [Architecture](01-architecture.md) | The big picture, trust boundaries, data stores, the model boundary |
| 02 | [Getting Started](02-getting-started.md) | Install and run — workstation and server profiles |
| 03 | [Configuration](03-configuration.md) | `sonder.toml`, secrets, every `SONDER_*` variable, precedence |
| 04 | [CLI & Entry Point](04-cli-and-entrypoint.md) | `python -m sonder_runtime <command>` reference |
| 05 | [HTTP API & Lifecycle](05-http-api-and-lifecycle.md) | Endpoints, health/readiness, admission, drain, error envelope |
| 06 | [Memory & Learning](06-memory-and-learning.md) | Conversations, facts, lessons, recall, the outcome loop |
| 07 | [Agent, Autopilot & Fleet](07-agent-autopilot-fleet.md) | The tool loop, durable automation, ownership, state machines |
| 08 | [Model Tiers & Gateway](08-model-tiers-and-gateway.md) | Tiers, routing lanes, the ModelGateway port, portable models |
| 09 | [Security Model](09-security-model.md) | Guardrails, consent gates, workspace containment, redaction |
| 10 | [Tools & Languages](10-tools-and-languages.md) | `run_code` (15 languages), `data_inspect`, guarded file tools |
| 11 | [Speculation & Prediction](11-speculation-and-prediction.md) | Branch prediction, speculative execution, file prefetch, prewarm |
| 12 | [Backups & Recovery](12-backups-and-recovery.md) | Consistent backups, retention, restore, restore-smoke |
| 13 | [Update Manager](13-update-manager.md) | Signed engine distribution, TUF, staged install, rollback |
| 14 | [Package Architecture](14-package-architecture.md) | The domain/application/adapters/platform layering & enforcement |
| 15 | [Training](15-training.md) | Adapter training, evaluation, gated promotion, rollback |
| 16 | [Glossary](16-glossary.md) | Terms, aliases, and identifiers |
| 17 | [Benchmarking](17-benchmarking.md) | Measuring the runtime's lift over a bare model (prove the moat) |
| 18 | [Model Catalog](18-model-catalog.md) | Recommended models per job + capability routing (per VRAM) |
| 19 | [Model Requirements & Onboarding](19-model-requirements-and-onboarding.md) | What you must install, how to verify/select it, what happens when a model is absent |

## Operational tools

- `python -m sonder_runtime doctor` — one consolidated read-only health report
  (config, self-heal, memory quality, runtime policy, Ollama reachability).
- `python sonder_hardware.py [workload]` or MCP `hardware_profile` — enumerate
  Windows/Linux/macOS NVIDIA, AMD, Intel, Apple, and unknown display adapters,
  then report resident and conservative GPU+RAM/unified-memory model plans.
  Enumeration is not a claim that an Ollama/backend path is runtime-ready.
- `python scripts/benchmark_moat.py` — run the moat benchmark and emit a
  scorecard ([Benchmarking](17-benchmarking.md)).

## Companion docs

- [Runbooks](../runbooks/README.md) — contractor-executable operational procedures.
- [Security review](../security/README.md) — read-only audit of the sensitive surfaces.
- [Architecture decisions](../architecture/adr/) — ADR-001..008.
- [Program status](../architecture/PROGRAM-STATUS.md) — per-spec, per-phase implementation map.
- Root guides: [ARCHITECTURE.md](../../ARCHITECTURE.md), [TRAINING.md](../../TRAINING.md),
  [NPU.md](../../NPU.md), [SELFMOD.md](../../SELFMOD.md), [CLIENT.md](../../CLIENT.md),
  [MOBILE_HOST_CONTROL.md](../../MOBILE_HOST_CONTROL.md).

## The one-paragraph mental model

A client (Flutter app, OpenAI-compatible UI, REPL, or MCP) sends a
request. Sonder authenticates and admits it, assembles a grounded prompt
(system profile + retrieved facts and lessons + session history), routes
it to a **tier** (a local Ollama model, or a consented remote/cloud one),
optionally runs a **guarded tool loop** (inspect → act → validate), and
returns an answer with an **activity footer** of exactly what it did.
Outcomes you confirm (compiled, tests passed) become **lessons** that are
retrieved next time. All state is local SQLite; everything that could
leave the machine is a separate, explicit opt-in.
