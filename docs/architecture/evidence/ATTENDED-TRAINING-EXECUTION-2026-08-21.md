# Attended adaptive-training execution boundary — 2026-08-21

## Scope

This slice adds a typed application orchestration boundary for the next safe
adaptive-training step. `AttendedTrainingExecutionService` owns sequencing and
the attended-only rule. Process launch, exclusive filesystem lock ownership,
signed manifest verification, durable deployment journal, Ollama policy
mutation, and deployment/rollback are ports supplied by adapters.

The existing immutable reproducible manifest digest and the existing attended
health-gated deployment/rollback contract are reused conceptually through
ports. No root `adaptive_training.py` lifecycle code, API-003 file, Git
metadata, or concrete subprocess/filesystem/Ollama adapter was moved.

## Safety behavior

- Attendance and signed manifest evidence are checked before lock acquisition
  or process launch.
- A non-zero process result is journaled as failed and cannot mutate policy.
- Policy reservation precedes activation; activation is explicitly attended.
- Activation failure attempts policy restoration and attended rollback.
- Recovery failure is journaled as `recovery_required`; it is never reported as
  a successful deployment.
- The lock port owns acquisition and release through a context manager.

## Evidence

- `sonder_runtime/application/ports/training.py`
- `sonder_runtime/application/training/attended_execution.py`
- `tests/test_attended_training_execution.py`
- `tests/test_adaptive_training_boundary.py`
- `tests/test_train006_route_activation.py`
- `tests/test_qlora_train.py`
- `tests/test_ollama_lifecycle.py`

Focused result: **47 passed, 1 skipped**. The skip is the optional installed
Trainer/PEFT integration test because the training-only dependencies are not
installed in this environment.

## Limitations

This is `implemented_unverified` evidence. The new ports are an adapter seam,
not proof that the legacy Windows process launcher, filesystem lock, signed
manifest store, deployment journal, Ollama policy mutator, and recovery adapter
are all wired to this service in production. Full repository regression,
platform-specific process receipts, and durable crash-recovery tests remain
outstanding. Formal master-spec checkboxes remain unchanged.
