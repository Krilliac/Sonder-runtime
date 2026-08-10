# SPEC-5 Migration Runbook

## Summary

SPEC-5 implements the End-State Architecture for sonder-runtime through
13 work packages (WP0–WP12) on the `spec5/implementation` branch.

## Work Package Status

| WP | Title | Status | Commit |
|---|---|---|---|
| WP0 | Freeze baseline and publish SPEC-5 | Done | e79d7d5 |
| WP1 | Final package skeleton and composition root | Done | (see git log) |
| WP2 | Schema epoch 2 + persistence foundations | Done | |
| WP3 | Model routing and gateway completion | Done | |
| WP4 | Memory and learning | Done | |
| WP5 | Tool service and execution | Done | |
| WP6 | Automation and agents | Done | |
| WP7 | Self-modification domain | Done | |
| WP8 | Training and immutable deployment | Done | a6a50fa |
| WP9 | Updates bounded domain | Done | a6a50fa |
| WP10 | Thin interfaces + MCP v2 | Done | 7ccad68 |
| WP11 | Legacy deletion | Done | 1734efe |
| WP12 | Release hardening | Done | (this commit) |

## Architecture Layers Implemented

```
domain/        Pure business rules, no I/O
application/   Use cases, ports, context
adapters/      Infrastructure implementations
interfaces/    Thin protocol handlers (HTTP, MCP, CLI, REPL)
platform/      Cross-cutting runtime concerns
bootstrap/     Composition root, capabilities
```

## New Domain Modules

- `domain/routing/route_planner.py` — Pure deterministic lane→tier→model routing
- `domain/tools/descriptors.py` — ToolEffect, ExecutionClass, ToolDescriptor, ToolCall, ToolResult
- `domain/tools/policy.py` — GuardedToolPolicy, UnrestrictedToolPolicy
- `domain/automation/models.py` — RunKind, RunStatus state machine, CAS Claim
- `domain/selfmod/models.py` — SelfmodPhase lifecycle, Snapshot
- `domain/training/models.py` — TrainingPhase, DatasetIdentity, ModelIdentity, Deployment
- `domain/updates/models.py` — UpdatePhase, ReleaseMetadata, UpdateRun

## New Application Services

- `application/memory/recall_service.py` — Lexical + semantic + MMR recall
- `application/memory/outcome_service.py` — Atomic outcome recording with outbox
- `application/execution/tool_service.py` — Descriptor→policy→execute pipeline
- `application/automation/automation_service.py` — CAS-based claim/lease protocol
- `application/selfmod/selfmod_service.py` — Guarded lifecycle state machine
- `application/training/training_service.py` — Attended-only training, immutable deployment
- `application/updates/update_service.py` — TUF verification, drain, health gates
- `application/errors.py` — Error re-exports for interface boundary

## New Interface Modules

- `interfaces/http/handlers.py` — Thin HTTP handlers with error mapping
- `interfaces/mcp/handlers.py` — Thin MCP tool handlers
- `interfaces/cli/commands.py` — Thin CLI commands
- `interfaces/repl/handler.py` — Thin REPL dispatcher

## Key Invariants

1. **OperationContext** flows through every privileged call
2. **No interface imports domain directly** — errors re-exported via application/errors.py
3. **adapters/legacy/ deleted** — adapters relocated to proper paths
4. **Architecture checker passes** — layer dependency rules enforced
5. **RuntimeCapabilities frozen at startup** — never re-read
6. **DomainEvent requires sequence** — no default value
7. **Training is attended-only** — autonomous start forbidden
8. **Deployment is DeploymentService-only** — candidate cannot self-activate
9. **Update activation requires verification AND health check**
10. **GuardedToolPolicy downgrades host→container** by default

## Test Evidence

- 211 SPEC-5 unit tests across 10 test files
- 235 production tests (architecture, backup, updates, etc.)
- Full suite (~5,000+ tests) baseline maintained
- Architecture checker: 0 violations

## Migration Notes

- `bootstrap/app.py` (old composition root) remains functional for existing
  entry points. New code should use `bootstrap/container.py` (build_runtime).
- Root legacy modules (runtime_policy, autopilot_store, etc.) remain as
  production dependencies. The adapter layer wraps them behind ports.
- The `BASELINE_ROOT_LEGACY_MODULES` ratchet in the architecture checker
  tracks which root modules are still referenced from adapters.
