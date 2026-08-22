# Context, model, and repository composition — 2026-08-21

This batch adds provider-neutral production boundaries for three typed areas:

- `ContextPlanningFacade` composes one context plan, section budgets,
  priority/eviction decisions, explainable selection, measured hardware caps,
  immutable prefix/replay manifests, last-good snapshots, and bounded overflow
  recovery. It is wired into both `Application` and the explicit runtime graph.
- `ModelGatewayFacade` composes the typed gateway with logical role routes,
  capability-routing injection, stable provider health publication, and local
  fail-closed delegation. The runtime graph uses the packaged inference
  factory, retaining explicit backend injection and no live provider claim.
- `RepositoryIntelligenceFacade` composes bounded index deltas, ranked maps,
  progressive evidence expansion, multi-root navigation, and injected LSP
  providers. The CLI exposes an injectable JSON inspection command.

The repository-intelligence package now exports its facade lazily to avoid a
port/package initialization cycle. Lazy command-catalog compatibility calls
use `importlib` so the MODEL-001 caller ratchet does not treat a deferred
server edge as a package import.

Evidence:

- `sonder_runtime/application/context_integration.py`
- `sonder_runtime/application/context_manifests.py`
- `sonder_runtime/application/model_gateway/facade.py`
- `sonder_runtime/application/model_gateway/health_and_roles.py`
- `sonder_runtime/adapters/runtime_container.py`
- `sonder_runtime/application/repository_intelligence/facade.py`
- `sonder_runtime/application/ports/repository_intelligence.py`
- `sonder_runtime/interfaces/cli/commands.py`
- `tests/test_context_planning_facade.py`
- `tests/test_repository_intelligence_boundary.py`
- `tests/test_model001_caller_boundary.py`

Focused and composition suites passed. Limitations remain: no live LSP
server, filesystem scanner, Tree-sitter backend, external model provider, or
hardware discovery was run; formal verification is not claimed.
