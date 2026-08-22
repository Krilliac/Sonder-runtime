# Adaptive-training planning boundary — 2026-08-21

## Scope

The hardware-aware planning slice was extracted from `adaptive_training.py`
into `sonder_runtime/application/training/hardware_planning.py`. The slice
owns typed plan options, hardware budgets, model estimates, fail-closed
selection, and human-readable plan/hardware formatting.

The root module now exposes compatibility names that delegate to this
application boundary. Process launch, training state, adapter validation,
deployment, alias ownership, and rollback remain in `adaptive_training.py`;
they require separate review because they cross subprocess, filesystem, and
runtime-policy seams.

## Evidence

- `tests/test_adaptive_training_boundary.py`: typed identity, side-effect-free
  planning, and CPU-offload fail-closed coverage.
- `tests/test_adaptive_training.py`: 82 passed after delegation, preserving
  existing lifecycle and compatibility behavior.
- `sonder_runtime/application/training/hardware_planning.py`: canonical
  application ownership.

## Verification

The focused boundary tests and the complete existing adaptive-training test
module pass. This is implementation evidence only; formal requirement
promotion remains `implemented_unverified`.

## Remaining blocker

The remaining root-owned lifecycle is not safe to move in this slice because
it combines attended subprocess launch, signed manifests, filesystem locks,
deployment journals, Ollama policy mutation, and rollback recovery. Those
seams need a separate typed port and receipt plan rather than a mechanical
module copy.
