# Package Architecture (SPEC-3)

The runtime is being restructured from a flat module set into
`sonder_runtime/`, a layered modular monolith with **enforced** dependency
direction — without changing public behavior. This is an incremental
strangler migration: each root module stays as a thin delegating surface
until callers move.

## Layers

```
sonder_runtime/
  domain/        pure business rules — stdlib + domain only
    common/        errors (taxonomy), ids, time
    runtime_policy/ rules (tiers, routing, NPU validation)
    memory/        rules (reward pricing, recall threshold, MMR)
    execution/     policy (permission evaluation)
    automation/    state_machine (autopilot/fleet transitions)
  application/   use cases + ports + operation context
    context.py     OperationContext (principal, deadline, cancellation, consent)
    ports/         ModelGateway, repositories, ToolExecutor, EventSink, Clock, ProcessProbe
    chat/          ChatService
    runtime_policy/ policy use cases
  adapters/      implement the ports
    ollama/        OllamaGateway (ModelGateway)
    filesystem/    atomic JSON + cross-process file lock
    legacy/        wrap root modules behind ports during migration
  platform/      cross-cutting infra (config, paths, version, metrics, shutdown)
  bootstrap/     composition root (build_application) + entry point
```

## Dependency direction

```
domain  ← application ← adapters
                     ← bootstrap → (adapters, application, platform)
platform  cross-cuts (imported by adapters/bootstrap)
```

- **Domain** imports only domain + stdlib. No I/O, no environment, no
  threads, no SQLite, no network — enforced.
- **Application** imports domain + ports. No adapters, no bootstrap.
- **Adapters** implement ports; may reach the root legacy modules and
  platform during migration.
- **Bootstrap** assembles one `Application` graph lazily — importing it
  creates no directories, opens no databases, reads no mutable env, starts
  no threads.

## Operation context

`application/context.py` carries principal, auth level, source, deadline,
cancellation, workspace roots, and consent through every privileged entry
point. A single audited factory builds the single-owner local context.
`principal_id` defaults to one owner — the identity seam exists without
multi-user authorization ([ADR-005](../architecture/adr/ADR-005-operation-context.md)).

## Ports & the error taxonomy

Ports (`application/ports/`) return domain errors, never `urllib`,
`sqlite3`, `OSError`, or HTTP exceptions. The taxonomy
(`domain/common/errors.py`): `InvalidInput`, `Unauthenticated`,
`Forbidden`, `NotFound`, `Conflict`, `ConcurrencyConflict`,
`CapacityExceeded`, `DependencyUnavailable`, `DeadlineExceeded`,
`Cancelled`, `IntegrityFailure`, `MigrationRequired`, `InternalFailure`.
Adapters map these to protocol-specific errors at the edge.

## Enforcement (CI-blocking)

`scripts/check_architecture.py` parses imports with `ast` and fails CI on:
forbidden layer edges, package cycles, `sqlite3.connect` outside adapters,
`subprocess` outside adapters, network modules outside adapters, and
environment reads in domain/application. A meta-test proves the checker
actually catches violations.

## Migration status

Done: composition root; runtime-policy, memory, execution, and automation
pure-rule extraction; the Ollama ModelGateway adapter; the first chat
call-site (summarization/titling) migrated onto the port.

Remaining: memory/execution/automation *repositories* behind ports,
training extraction, thin-transport reduction of `server.py`/
`sonder_serve.py`, and removal of internal legacy imports. Live status:
[PROGRAM-STATUS](../architecture/PROGRAM-STATUS.md).

## Why this shape

The rationale is recorded as ADRs
([001–008](../architecture/adr/)): a modular monolith over microservices,
Ollama as an external inference service, per-domain SQLite, ports &
adapters, explicit operation context, no ORM, compatibility shims, and
local domain events without a broker.
