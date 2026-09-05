# Managed standalone controller hook

Trusted bootstrap may install `managed_controller_factory_scope(factory)` around
creation of a root StandaloneLaneController. The factory is captured only at zero
controller and model-loop depth. Nested loops/controllers cannot capture managed
authority. This is a private host context API, never a model/session argument.

On first initialization the controller calls `factory(controller, application)`
before creating any legacy local-owner context or parent bearer. The result must
implement ManagedControllerSession: admitted `context`, `require_current()`,
`dispatch(PreparedLaneCommand)`, `report_metadata()`, `request_cancel()`, `close()`,
and `verify_delegated(HostTerminalDraft, verifier_factory=...)`. Initialization
failure is sticky; repeated commands cannot retry an ambiguous factory effect or
fall back to legacy authority. Failed initialized sessions are closed best effort.

Commands retain their controller-owned immutable encoded snapshots. Managed
dispatch passes the snapshot directly to the trusted session, which owns exact
approval decoding and dispatch. The controller stores no managed parent bearer.
Preparation, dispatch and host terminal evidence boundaries require current
managed authority. `controller.require_current()` is available for the root's
model/speculation/tool admission guards, which this slice does not wire.

Managed verification passes the frozen original HostTerminalDraft to the session
and accepts only a typed VerificationVerdict with a bool validity field. The
session owns exact certificate validation, durable pending identity and the
reviewed typed approval bridge; the legacy string-approval callback is not used.
Exceptions return verification unavailable, never certified fallback.

Cancellation is forwarded once via the session's independent private cancellation
path; it must not rely on preparing fresh commands through the cancelled
controller. Close is forwarded once and does not cancel independent children.
Metadata failures report unavailable. Host scope restoration runs even if close
fails. Outside a managed factory scope, existing legacy behavior remains.
