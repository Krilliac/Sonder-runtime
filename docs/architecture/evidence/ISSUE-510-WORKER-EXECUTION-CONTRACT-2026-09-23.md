# Issue #510 worker execution contract evidence

Status: `implemented_unverified`

This slice adds a typed `WorkerExecutionContract` to delegated worker requests.
It records bounded success criteria and deterministic argv verification commands,
persists them in the existing durable child-session request, restores them after
restart, and makes `DelegationService.integrate` reject successful results when
criteria or command identity do not match the persisted contract. Terminal
verification retains the criteria and commands alongside the result evidence.

Evidence:

- `sonder_runtime/application/ports/worker_registry.py`
- `sonder_runtime/application/agents/lineage_delegation.py`
- `sonder_runtime/application/agents/delegation_service.py`
- `sonder_runtime/application/worker_registry/continuation.py`
- `tests/test_continuation_worker_registry.py`
- `python -m pytest tests/test_worker_registry.py tests/test_remaining_agent_004_008_009.py tests/test_continuation_worker_registry.py -q` (`21 passed`)
- `python -m pytest tests/test_delegated_verification.py tests/test_remaining_agent_010.py tests/test_workflows.py -q` (`46 passed`)

Limitations:

- The contract validates and records exact verification command argv; this slice
  does not execute those commands. A trusted verifier still supplies the evidence
  and remains responsible for running the commands in the appropriate workspace.
- Hosted CI, external provider qualification, and post-merge evidence remain
  unverified.
