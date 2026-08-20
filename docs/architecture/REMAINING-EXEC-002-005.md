# Remaining EXEC-002 / EXEC-005 — guarded execution worlds

This slice closes the concrete provider gap behind the existing execution
world and sandbox ports. It does not alter the master checklist or its audit.

## EXEC-002 — container default

`GuardedContainerProvider` is the explicit default provider. It accepts only
`SandboxWorldKind.CONTAINER`, requires a configured image, and rejects a
different requested image. It has no host fallback. The returned world carries
stable provider/world identity and the declared execution capabilities.

The reference adapter intentionally fails closed for subprocess, shell, and
terminal operations until a verified container transport is injected. A
lifecycle result is not execution proof: callers must not infer that a
container boundary exists merely because provisioning succeeded.

## EXEC-005 — configured remote worker boundary

`ConfiguredRemoteWorkerProvider` requires an HTTPS endpoint, non-empty worker
identity, and a non-empty declared capability set. Provisioning requires a
remote world request whose endpoint exactly matches the configured endpoint.
The returned world preserves endpoint, worker identity, provider identity, and
capabilities; mismatches reject before any transport could be contacted.

The current implementation is a transport-free reference boundary. Operations
fail closed rather than executing locally. A future remote adapter can replace
the fail-closed services while retaining these identity and capability checks.

## Evidence

`tests/test_remaining_execution_world_defaults.py` covers the guarded
container default, no-host-fallback behavior, remote endpoint/identity/capability
requirements, and cleanup evidence.
