# SEAM-005 — bounded local sandbox capability integration

## Scope

This slice connects the typed `SandboxProvider` port to
`sonder_runtime.adapters.sandbox.LocalSandboxProvider` for `local` and
`read_only` worlds. The adapter only
provides an existing, operation-root-contained workspace path capability and
finite resource accounting. It does not execute code, spawn processes, start
containers, or contact a remote worker.

## Truthful isolation

The world reports `FAILURE_ISOLATION_ONLY` with the rationale that the local
workspace capability is bounded but OS isolation is unverified. No security
boundary is claimed. Execution surfaces are backed by the existing
fail-closed reference execution world with no declared capabilities, so an
unsupported shell/process request cannot fall back to host execution.

## Bounds and lifecycle

- Workspace provisioning requires an existing directory and, when supplied,
  containment within `OperationContext.workspace_roots`.
- Path resolution rejects workspace escapes and enforces maximum path and
  existing-file byte limits.
- Active resource accounting is bounded by `max_active_resources`.
- Cleanup reports `quiescent=False` while resources remain, and only reports
  quiescence after resources are released. It remains retryable and rejects
  path access after quiescence.
- Unsupported world kinds, missing workspaces, cancelled/expired contexts, and
  unsupported execution transports fail closed.

## Verification

```text
python -m pytest -q tests/test_seam005_local_sandbox_integration.py tests/test_sandbox_provider_port.py tests/test_seam004_execution_world_integration.py
python -m compileall -q sonder_runtime/application/ports/sandbox.py sonder_runtime/adapters/execution/local_sandbox.py
python scripts/check_architecture.py
python scripts/check_evidence_documents.py
git diff --check
```

The focused tests prove the capability boundary and lifecycle behavior; they
do not prove a kernel, container, or OS security boundary.
