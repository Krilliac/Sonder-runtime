# Operations, evaluation, and memory composition — 2026-08-21

This batch wires existing typed contracts into production-facing boundaries:

- HTTP lifecycle now carries bounded `OperationContext`, shared admission,
  typed health, process-local redacted tracing, telemetry snapshots, and
  graceful-drain admission closure.
- Evaluation has a provider-neutral application service for suite identity,
  corpus coverage, trajectory replay, proposal transitions, shadow/canary
  observations, promotion evidence, and attended promotion/rollback.
- The application graph exposes `MemoryLearningFacade`, which owns unit-of-work
  scoping for recall and atomic outcome/outbox writes, and exposes typed memory
  policy, retrieval evaluation, and procedural-promotion seams.
- REPL web-intent callers now use a packaged lazy compatibility provider.

Evidence includes:

- `sonder_runtime/adapters/web/lifecycle.py`
- `sonder_runtime/interfaces/http/serve.py`
- `sonder_runtime/application/evaluation/service.py`
- `sonder_runtime/application/ports/evaluation.py`
- `sonder_runtime/application/memory/facade.py`
- `sonder_runtime/application/ports/memory.py`
- `sonder_runtime/bootstrap/app.py`
- `sonder_runtime/adapters/memory_repository.py`
- `sonder_runtime/adapters/memory_store.py`
- `sonder_runtime/adapters/web_intents.py`
- `tests/production/test_lifecycle_http.py`
- `tests/test_evaluation_application_boundary.py`
- `tests/test_memory_learning_facade.py`
- `tests/test_web_intents_boundary.py`

The combined focused verification passed 169 tests; architecture, compilation,
documentation, and evidence gates passed. These are local contract and
composition results only: external evaluator/provider operation, platform
process receipts, full deployment recovery, and formal verification remain
outstanding. No synthetic telemetry or cleanup receipt is introduced.
