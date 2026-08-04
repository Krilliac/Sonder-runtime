# Architecture

Sonder is a **modular monolith**: one deployable process with strong
internal boundaries, not a microservice fleet. It targets a single
private owner on a workstation or a self-hosted Linux server. Ollama runs
as a separate loopback service.

## The model boundary (read this first)

Sonder is **not** a foundation model and ships **no** base-model weights.
[Ollama](https://ollama.com) stores model artifacts, loads the selected
weights into RAM/VRAM, and performs token inference. Sonder sits around
that server:

```
 client → Sonder (routing, prompt, memory, tools, policy) → Ollama (inference)
```

`sonder:latest` and `sonder-personal:latest` are **Ollama aliases**, not
files in this repository. See [ADR-002](../architecture/adr/ADR-002-ollama-external.md).

## Components

| Component | Role |
|---|---|
| `sonder_serve.py` | OpenAI-compatible HTTP adapter, health endpoints, admission |
| `server.py` | The orchestration core: prompts, tiers, tools, the agent loop, MCP |
| `sonder_repl.py` | Interactive REPL |
| `memory_store.py` | Owns `memory.db` — conversations, facts, lessons, embeddings |
| `autopilot_store.py` / `fleet_store.py` | Durable automation state (`autopilot.db`, `fleet.db`) |
| `runtime_policy.py` | Hot-reloadable model/routing/NPU policy (`runtime_policy.json`) |
| `sonder_config.py` | Typed, fail-closed production configuration |
| `sonder_lifecycle.py` | Process/dependency state, admission, drain, metrics |
| `sonder_operations_store.py` | `operations.db` — audit events, backup runs, maintenance locks |
| `sonder_backup.py` | Consistent backups and restore |
| `sonder_updates.py` / `sonder_update_engine.py` | Signed engine distribution & staged install |
| `sonder_speculation.py` | Branch prediction & speculative execution |
| `sonder_runtime/` | The SPEC-3 layered package (domain/application/adapters/platform/bootstrap) |
| `app/` | Flutter client |

## Data stores (all local SQLite, under `SONDER_HOME`)

| File | Owner | Contents |
|---|---|---|
| `memory.db` | memory_store | turns, summaries, facts, lessons, embeddings, outcomes |
| `autopilot.db` | autopilot_store | autopilot run/task state, ownership, heartbeats |
| `fleet.db` | fleet_store | fleet worker/task state |
| `operations.db` | sonder_operations_store | audit events, backup runs, maintenance locks |
| `updates.db` | sonder_updates | update plans, step journal, installed releases, trusted roots |

Each store has one repository owner and its own checksummed migration
ledger ([ADR-003](../architecture/adr/ADR-003-sqlite-per-domain.md)).
There are no cross-store transactions; cross-store operations use explicit
saga/transition records (model promotion, update plans).

## Trust boundaries

- **Loopback by default.** The HTTP server binds `127.0.0.1`. Non-loopback
  binding without a TLS-terminating proxy declaration **and** a strong API
  key is rejected before any socket opens.
- **Consent gates.** Cloud models, web tools, and remote-Ollama endpoints
  are each a separate explicit opt-in; the default is fully local.
- **Guardrails are host-enforced.** Permission rules, workspace
  containment, and tool policy live in the runtime, independent of the
  model. See [Security Model](09-security-model.md).

## Request lifecycle (chat)

1. **Admit** — correlation ID, auth, auth-failure limiter, body/concurrency
   limits, drain/maintenance check ([HTTP & Lifecycle](05-http-api-and-lifecycle.md)).
2. **Assemble** — system profile + retrieved facts/lessons + session
   history ([Memory & Learning](06-memory-and-learning.md)).
3. **Route** — pick a tier/model ([Tiers & Gateway](08-model-tiers-and-gateway.md)).
4. **Execute** — direct completion, or a guarded tool loop
   ([Agent](07-agent-autopilot-fleet.md)).
5. **Ground & learn** — capture the interaction; a confirmed outcome
   distills a lesson.
6. **Report** — answer plus an activity footer of observable actions.

## Layered package (SPEC-3)

The runtime is being restructured into `sonder_runtime/` with enforced
dependency direction (domain ← application ← adapters/bootstrap; platform
cross-cuts). CI blocks forbidden imports, cycles, and I/O in the pure
layers. See [Package Architecture](14-package-architecture.md).

## Design decisions

The reasoning behind these choices is recorded as ADRs:
[modular monolith](../architecture/adr/ADR-001-modular-monolith.md),
[external Ollama](../architecture/adr/ADR-002-ollama-external.md),
[per-domain SQLite](../architecture/adr/ADR-003-sqlite-per-domain.md),
[ports & adapters](../architecture/adr/ADR-004-ports-and-adapters.md),
[operation context](../architecture/adr/ADR-005-operation-context.md),
[no ORM](../architecture/adr/ADR-006-no-orm.md),
[compat shims](../architecture/adr/ADR-007-compat-shims.md),
[local events](../architecture/adr/ADR-008-local-events.md).
