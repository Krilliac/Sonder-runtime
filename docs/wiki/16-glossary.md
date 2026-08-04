# Glossary

**Abliterated** — an open-weight model with its built-in refusal behavior
removed. Changes what the model will *discuss*; does not weaken Sonder's
host-enforced guardrails ([Security Model](09-security-model.md)).

**Activity footer** — the block appended to a response listing observable
actions: model calls, tool calls, files changed, token counts.

**Adapter (SPEC-3)** — a class implementing an application **port** over a
concrete technology (e.g. `OllamaGateway` implements `ModelGateway`).

**Admission** — the per-request gate: correlation ID, auth, limiter,
body/concurrency limits, drain/maintenance check
([HTTP & Lifecycle](05-http-api-and-lifecycle.md)).

**Autopilot** — durable, restart-safe autonomous goal runs
([Agent, Autopilot & Fleet](07-agent-autopilot-fleet.md)).

**Branch predictor** — learns which tool/tier follows a loop state, to
drive speculation ([Speculation & Prediction](11-speculation-and-prediction.md)).

**Consent gate** — an explicit, default-off opt-in for a capability that
could leave the machine: cloud models, web tools, remote Ollama, location.

**Domain layer** — pure business rules (`sonder_runtime/domain/`): no I/O,
no environment, no SQLite ([Package Architecture](14-package-architecture.md)).

**facts.** — a portable offline AI USB (Qwen 3.x 4B, llama.cpp) from Open
Source Everything; imported as a Sonder tier
([use-facts-model](../runbooks/use-facts-model.md)).

**Fleet** — parallel worker execution for fan-out work (`fleet.db`).

**GGUF** — the quantized model file format llama.cpp/Ollama load.

**Lesson** — a short, reusable takeaway distilled from a *successful*
interaction, retrieved into later prompts.

**Lane / routing lane** — a named execution path (`router`, `workbench`,
`autopilot`, `fleet`, `review`) mapped to a tier by runtime policy.

**Maintenance lock** — a named lock in `operations.db` serializing
backup / restore / migration / promotion / update.

**MMR** — Maximal Marginal Relevance; reranks retrieval to suppress
near-duplicates ([Memory & Learning](06-memory-and-learning.md)).

**ModelGateway** — the port all model transport goes through; enforces the
cloud-consent gate and maps driver errors to the domain taxonomy.

**Ollama** — the external model server that stores weights and runs
inference. Sonder orchestrates around it; it is not part of this repo.

**OperationContext** — the frozen per-request context (principal, deadline,
cancellation, consent, workspace roots) threaded through privileged code.

**Port (SPEC-3)** — a Protocol interface the application depends on;
adapters implement it ([ADR-004](../architecture/adr/ADR-004-ports-and-adapters.md)).

**Preflight** — startup checks that gate every listener; a failed required
check means no socket opens.

**Recall** — semantic retrieval of prior good-outcome solutions, project-
scoped by default.

**Runtime policy** — hot-reloadable JSON selecting aliases/lanes/NPU modes;
cannot widen network/filesystem/credential/cloud permissions.

**Speculative execution** — running a predicted **read-only** tool during
model generation and retiring it on a matching decision, else squashing.

**Tier** — a named model role (`fast`, `code`, `general`, `cloud-*`)
resolving to an Ollama model.

**TUF** — The Update Framework; the signed-metadata trust chain for updates
([Update Manager](13-update-manager.md)).

**Aliases:** `sonder:latest` (the active serving model), `sonder-personal:latest`
(the personalization/training target). Both are Ollama aliases, not repo files.

**State paths:** `SONDER_HOME` (all local state), `memory.db`,
`autopilot.db`, `fleet.db`, `operations.db`, `updates.db`,
`runtime_policy.json`.
