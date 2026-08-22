# SEAM-004 execution-world integration evidence

## Scope

This bounded slice unifies the existing safest concrete provider,
`ReferenceExecutionWorld`, behind the typed `ExecutionWorld` port returned by
the guarded container and configured remote sandbox providers. It does not
add a process, shell, container, or remote transport.

## Evidence

- `ReferenceExecutionWorld` is the single owner of typed subprocess, shell,
  and terminal services exposed through `SandboxWorld.execution_world`.
- Provider-declared `WorldCapability` values are checked before any operation;
  undeclared capabilities fail closed.
- The provider reports `FAILURE_ISOLATION_ONLY`; no security-boundary claim is
  manufactured from lifecycle cleanup or provider identity.
- Unsupported transports continue to raise `WorldUnavailable` instead of
  falling back to host execution.
- `cancel()` requests shutdown, while `cleanup()` is the idempotent barrier;
  operations after quiescence are rejected.

## Verification

`tests/test_seam004_execution_world_integration.py` covers typed owner
identity, capability rejection, isolation truth, unsupported behavior, and
cleanup ordering. The existing execution-world and sandbox contract suites
remain the regression set for the underlying ports.
