# Explicit managed host recovery

`bootstrap.managed_standalone.ManagedStandaloneRecovery` is a private host
composition coordinator. It takes the existing controller/application, a fresh
unshared LaneContinuationService with trusted original projection codec, current
authenticated context and exact selected host conversation, live private/model
path callbacks, and separate `approve_attachment`/`approve_verification` callbacks.
The host selector remains responsible for live authenticated selection; persisted
principal/IDs, old bearers and model arguments cannot construct this authority.

Call `prepare(continuation_id, command_id=...)` to obtain an issuer-bound immutable
PreparedManagedReattachment. Its `approval_payload()` is the original backend
PreparedReattachment payload. `execute(prepared)` calls the existing durable
reattachment/approval state machine and builds a ManagedStandaloneSession around
the returned BoundContinuation. It never opens a new parent. Live owners refuse;
only existing kernel owner-lock evidence can admit takeover. Concurrent calls on
one coordinator serialize. Proven typed pending approval permits exact retry;
other failed/ambiguous attempts require explicit inspection/new host coordination,
and cannot be silently replayed by the same coordinator. After successful return,
identical retry returns the same still-current session.

Fresh ManagedStandaloneSession constructor behavior is unchanged. Recovered
sessions expose `original_terminal_draft()` from the exact stored codec projection
and `recovery_verification(verifier_factory=...)`, which returns immutable
ManagedVerificationRecovery(identity, original prepared, phase, code), or None
when no pending link exists. No empty draft or fresh verification fingerprint is
synthesized. Missing original evidence remains unavailable.

`resume_pending_verification(identity, verifier_factory=...)` accepts only the
exact current original PendingVerificationIdentity. Only approval_pending invokes
the reviewed verifier.resume_pending_approval path, preserving its original
bundle/ledger identity and fresh attachment epoch permit. Certified state receives
fresh validation and immutable terminal publication. Admitted, approval_deciding,
approved, running, incomplete and unknown states remain observational: no automatic
reconciliation, gate call, process launch, consumed-approval replay or proof
reconstruction. Original parent evidence and complete current child certificate
remain enforced by HostTerminalPublisher/TerminalResultCodec.

Recovered `verify_delegated(draft, ...)` refuses model-loop finalization. The host
must explicitly invoke the recovery method; caller-supplied replacement text or
an empty new turn cannot overwrite the original terminal projection. Normal bound
child controls and metadata still use current attachment authority. Close detaches
without implicitly cancelling independent children. This slice adds no HTTP,
REPL UI, app route or server loop wiring, and makes no multi-host ownership claim.

Tests use the real continuation repository and actual ApprovalLedger/permission
bridge across a closed original host and a fresh host service/codec. The verifier
gateway is deterministic simulated execution for composition tests; it does not
claim a new real subprocess acceptance run. Existing lower-layer subprocess and
ownership tests remain separate evidence.
