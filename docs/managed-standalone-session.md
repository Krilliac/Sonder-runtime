# Managed standalone session composition

`ManagedStandaloneSession` consumes a fresh host-owned continuation service and
an authenticated host context. It rejects exposed private-state paths before
allocating a parent, immediately registers that parent, discards the bearer, and
retains only its bound continuation. The controller's immutable prepared command
must match its exact owner, run, principal, and workspace snapshot. Internal list
and cancellation commands have a separate private issuer.

Closing the session detaches the host; independent children retain their bounded
grants. Explicit cancellation records child cancellation through bound control.
Every dispatch and metadata read requires the live host admission guard.

Terminal verification requires the original host draft and passing original
parent evidence. It prepares the exact delegated bundle, durably links the
original answer before any approval callback, and uses the reviewed publisher
only after current verification succeeds. Ordinary repeated finalization cannot
resume a pending approval or run another check. A changed original draft is
refused, and stale published state is cleared before revalidation.

The supplied approval callback must be the trusted typed approval bridge; the
legacy string-receipt callback is not the production managed-session composition.
The factory must install complete live private-state inventory and authenticated
selection scopes. This module and the controller hook are integrated together in
tests; REPL factory installation, actual server model/tool guards, and an explicit
reattach/resume route remain separate integration work. Verification tests use
the real lane/verifier stores with a deterministic process-gateway fixture; they
do not constitute a real external model or process-execution acceptance run.

Managed server calls now check admission before and after normal/final/repaired
model generation and claim review, before speculative submission, inside each
speculative worker, and at direct tool dispatch. Wrappers delegate live model
metadata used by repair limits. Worker contexts preserve the original selection
and cancellation identities while using a separate Context object per call.
Nested model loops lose the lane controller but retain a separate revocation
guard, so nested ordinary model/tool work cannot escape a revoked parent.

These checks do not cancel an already-dispatched remote model request or undo an
effect already admitted. They prevent subsequent admission and reject results
returned after revocation. Existing transactional effect admission and process
cancellation/containment remain required.
