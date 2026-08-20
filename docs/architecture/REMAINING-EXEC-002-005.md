# Remaining execution containment slice

This isolated slice closes the contract gap around EXEC-002 and EXEC-005. It
does not execute a container, probe a remote endpoint, or claim that the
application boundary itself is a sandbox.

## EXEC-002 — Default guarded container contract

`GuardedContainerPolicy` makes the guarded defaults explicit: networking is
disabled, the root filesystem is read-only, new privileges are disabled, all
capabilities are dropped, execution is non-root, image pulls are not implicit,
and resource limits are bounded. `GuardedContainerContract` admits execution
only when a versioned healthy Docker/Podman capability and an immutable image
digest are supplied. An unavailable or unknown engine is rejected.

Admission without an independent evidence reference is labeled
`FAILURE_ISOLATION_ONLY`; the contract never turns a container name into a
security-boundary claim. A provider may attach an attestation/evidence
reference, after which the resulting claim can be presented as verified.

## EXEC-005 — Explicit remote worker boundary

`RemoteWorkerCapability` separates endpoint configuration from externally
reported health and advertised world capabilities. `RemoteWorkerBoundary`
performs no network probe and rejects unknown/unhealthy workers or world
mismatches. A healthy remote worker is accepted as an execution provider, but
its isolation truth remains `FAILURE_ISOLATION_ONLY` until independent evidence
is supplied.

## Evidence

- `tests/test_remaining_execution_containment.py` covers guarded defaults,
  digest and engine admission, remote capability/health boundaries, evidence
  promotion, weakened-policy rejection, and truthful failure isolation.
- The implementation has no infrastructure imports or side effects.
- Formal implementation-spec checkboxes remain untouched; this document is
  evidence for a contract slice, not a claim that concrete container and
  remote adapters are fully integrated.
