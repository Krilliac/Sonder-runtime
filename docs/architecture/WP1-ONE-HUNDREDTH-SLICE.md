# WP1 One-Hundredth Slice — canonical shutdown ownership

The full `CancellationToken` and `ShutdownCoordinator` implementation now
lives in `sonder_runtime.platform.shutdown`. The root `sonder_shutdown.py`
module is an identity-preserving compatibility shim for legacy callers.

Mutation admission, cancellation signalling, SIGTERM/SIGINT drain dispatch,
deadline handling, interrupted hooks, flush hooks, state transitions, and
idempotent concurrent drains are preserved. Packaged and legacy imports
resolve to the same class objects.

The `sonder_shutdown` architecture allowance was removed. The platform
boundary still imports `sonder_service_state`, which remains a separate live
compatibility boundary.
