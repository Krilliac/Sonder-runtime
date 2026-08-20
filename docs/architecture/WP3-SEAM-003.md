# WP3 SEAM-003: FileSystem application capability

This slice adds the application FileSystem port at
`sonder_runtime.application.ports.filesystem`. It is a contract-only seam;
the existing filesystem adapters are unchanged.

## Contract

`FileSystemResource` describes a path and resource kind without exposing an
open handle. `FileSystemRequest` carries the operation, optional destination,
content, confirmation/dry-run intent, optimistic version, and byte/entry
limits. The port supports `read`, `write`, `delete`, `list`, `stat`, and `move`.

`FileSystemPolicy` returns an explicit `ALLOW`, `DENY`, `CONFIRM`, or `DRY_RUN`
decision before the adapter touches a resource. `FileSystemObserver` receives a
bounded `FileSystemObservation` containing operation outcome, counts, version,
and a stable error code; it must not receive file contents or unbounded paths.

Requests are validated for non-negative limits, bounded write content, and
valid move destinations. Implementations must map native driver failures to
the domain/application error taxonomy (`InvalidInput`, `Forbidden`, `NotFound`,
`Conflict`, `CapacityExceeded`, `DependencyUnavailable`, or
`IntegrityFailure`) and must not leak `OSError`-family exceptions through the
port.

## Ownership and lifecycle

The adapter owns native descriptors and path-resolution state. Port results are
value objects and do not transfer ownership. Policy is a decision dependency,
not an implementation detail of the adapter; observations are append-only
evidence and are emitted once per attempted operation. The port is
thread-agnostic (`[any thread, async safe]`); adapters remain responsible for
their synchronization and cancellation behavior.

## Scope boundary

This WP3 slice does not migrate callers, register a provider, or alter
`sonder_runtime.adapters.filesystem`. Symlink/race-resistant resolution,
root authorization, atomic mutation, native exception translation, provider
health, and composition-root wiring remain follow-up adapter/lifecycle work.

## Verification

- `python -m pytest -q tests/test_filesystem_port_wp3.py`
- `python -m compileall -q sonder_runtime`
- `python scripts/check_architecture.py`
