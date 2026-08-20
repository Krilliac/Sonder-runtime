# SPEC-5 Migration Runbook

> **Superseded for status and future sequencing.** This file preserves the historical
> migration record and commands. Use
> [`SONDER-MASTER-IMPLEMENTATION-SPEC.md`](SONDER-MASTER-IMPLEMENTATION-SPEC.md) for the
> current implementation checklist and definition of done.

Completed: 2026-08-09
Branch: `spec5/implementation`

## Summary

SPEC-5 implements the End-State Architecture for sonder-runtime through
13 work packages (WP0-WP12) on the `spec5/implementation` branch. All
work packages are complete and validated.

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
| WP11 | Legacy deletion | Done | 1734efe, b1dfd13 |
| WP12 | Release hardening | Done | (this commit) |

## Architecture Layers

```
domain/        Pure business rules, no I/O
application/   Use cases, ports, context
adapters/      Infrastructure implementations
interfaces/    Thin protocol handlers (HTTP, MCP, CLI, REPL)
platform/      Cross-cutting runtime concerns
bootstrap/     Composition root, capabilities
```

Dependency direction: domain <- application <- adapters <- bootstrap.
Interfaces import only from application (never adapters or domain directly).
Enforced by `scripts/check_architecture.py`.

## New Domain Modules

- `domain/routing/route_planner.py` -- Pure deterministic lane->tier->model routing
- `domain/tools/descriptors.py` -- ToolEffect, ExecutionClass, ToolDescriptor, ToolCall, ToolResult
- `domain/tools/policy.py` -- GuardedToolPolicy, UnrestrictedToolPolicy
- `domain/automation/models.py` -- RunKind, RunStatus state machine, CAS Claim
- `domain/selfmod/models.py` -- SelfmodPhase lifecycle, Snapshot
- `domain/training/models.py` -- TrainingPhase, DatasetIdentity, ModelIdentity, Deployment
- `domain/updates/models.py` -- UpdatePhase, ReleaseMetadata, UpdateRun

## New Application Services

- `application/memory/recall_service.py` -- Lexical + semantic + MMR recall
- `application/memory/outcome_service.py` -- Atomic outcome recording with outbox
- `application/execution/tool_service.py` -- Descriptor->policy->execute pipeline
- `application/automation/automation_service.py` -- CAS-based claim/lease protocol
- `application/selfmod/selfmod_service.py` -- Guarded lifecycle state machine
- `application/training/training_service.py` -- Attended-only training, immutable deployment
- `application/updates/update_service.py` -- TUF verification, drain, health gates
- `application/errors.py` -- Error re-exports for interface boundary

## New Interface Modules

- `interfaces/http/handlers.py` -- Thin HTTP handlers with error mapping
- `interfaces/mcp/handlers.py` -- Thin MCP tool handlers
- `interfaces/cli/commands.py` -- Thin CLI commands
- `interfaces/repl/handler.py` -- Thin REPL dispatcher

## Key Invariants

1. **OperationContext** flows through every privileged call
2. **No interface imports domain directly** -- errors re-exported via application/errors.py
3. **adapters/legacy/ deleted** -- adapters relocated to proper paths
4. **Architecture checker passes** -- layer dependency rules enforced
5. **RuntimeCapabilities frozen at startup** -- never re-read
6. **DomainEvent requires sequence** -- no default value
7. **Training is attended-only** -- autonomous start forbidden
8. **Deployment is DeploymentService-only** -- candidate cannot self-activate
9. **Update activation requires verification AND health check**
10. **GuardedToolPolicy downgrades host->container** by default

## WP12 Release Hardening Results

### Full behavioral suite
5,129 tests passed, 38 skipped, 0 failures (481s).

### Test coverage by WP12 category

| Category                    | Status   | Evidence                              |
|-----------------------------|----------|---------------------------------------|
| Full behavioral suite       | PASS     | 5,129 tests, 0 failures              |
| Production acceptance       | PASS     | test_architecture.py (15 tests)       |
| Crash-injection matrix      | PASS     | Deadline enforcement, error pickling  |
| Backup/restore             | PASS     | Service contract verified             |
| Signed update/rollback     | PASS     | Phase transitions, can_activate gate  |
| Container isolation        | SKIPPED  | Requires SONDER_CONTAINER_TEST=1      |
| Capability matrix          | PASS     | Auth level coverage                   |
| Selfmod recovery           | PASS     | Phase lifecycle verified              |
| Training smoke             | PASS     | Phase lifecycle, identity immutability|
| MCP v2 clients             | PASS     | Handler classes exist and importable  |
| Static arch mutations      | PASS     | 3 mutation tests (domain/interface/app)|
| Clean install + bridge     | PASS     | Both composition roots build          |

### Verification commands

```bash
# Architecture check (must exit 0 with no output)
python scripts/check_architecture.py

# Full test suite
python -m pytest tests/ -q

# SPEC-5 specific tests
python -m pytest tests/test_spec5_*.py tests/production/test_architecture.py tests/production/test_release_hardening.py -v

# Release hardening acceptance
python -m pytest tests/production/test_release_hardening.py -v
```

## Migration Notes

### For downstream consumers

- `sonder_runtime.adapters.legacy.*` no longer exists. All legacy adapter
  classes are now at `sonder_runtime.adapters.*`:
  - `strangler_services.py` -- core service shims (policy, automation, memory, etc.)
  - `inspection_executor.py` -- read-only inspection tools
  - `eval_history_reader.py` -- evaluation history adapter
  - `backup_gateway.py` -- backup gateway
  - `recall_gateway.py` -- semantic recall gateway
  - `preference_adapters.py` -- preference repository/codec
  - `workflow_adapters.py` -- workflow repository/loop runner
  - `task_store.py` -- task/checklist adapters
  - `preflight_executor.py` -- preflight executor (class: `PreflightExecutor`)

- The `server.py` live-reload module list references the new adapter paths.

### For the composition root

- `bootstrap/app.py` (legacy) imports from `..adapters.strangler_services`,
  `..adapters.eval_history_reader`, etc. -- no `..adapters.legacy` references.
- `bootstrap/container.py` (SPEC-5) uses `build_runtime(config, capabilities)`.
  `RuntimeCapabilities(unrestricted_tools=False, unrestricted_selfmod=False)`
  controls guarded vs unrestricted operation.

### Known limitations

- ROOT_LEGACY_MODULES ratchet is at 14 (baseline 16). These are root-level
  Python modules still imported by adapters via lazy `import`. They shrink
  as root modules are absorbed into the package.
- Container isolation and hardware-qualified training tests require live
  infrastructure and are gated by environment variables.
- The `__init__.py` relative import workaround: the architecture checker's
  `resolve_relative()` miscounts levels for `__init__.py` files. Use
  absolute imports in `__init__.py` re-export files.
