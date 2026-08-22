# WP1 Fifty-Eighth Slice — packaged Ollama lifecycle adapter

## Scope

The Ollama process-lifecycle implementation now lives at
`sonder_runtime.adapters.ollama_lifecycle`. The server composition root imports
that packaged adapter directly for residency inspection and orphan-process
cleanup. The root `ollama_lifecycle.py` remains a compatibility import for
external callers while ownership is held by the package adapter.

This is a behavior-preserving boundary move. The Windows process identity
checks, trusted-root validation, grace-window sampling, model-runner safety
gates, and termination behavior were moved unchanged. No command-catalog,
persistence, launcher, HTTP/REPL, or strangler-services code was changed.

## Evidence

- Focused Ollama lifecycle and server unload regression tests: **15 passed**.
- `python -m compileall -q sonder_runtime server.py`: passed.
- `python scripts/check_architecture.py`: passed.
- `python scripts/check_requirement_evidence.py`: passed.
- Staged and working-tree whitespace checks: passed after staging this slice.

## Boundary decision

`server.py` remains responsible for the unload orchestration and its Ollama
transport calls. The adapter owns only the process-lifecycle policy and
platform interaction, keeping the stateful cleanup behavior out of the
composition root without changing its orchestration contract.
