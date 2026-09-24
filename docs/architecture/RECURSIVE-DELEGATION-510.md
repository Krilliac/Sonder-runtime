# Durable recursive delegation

`build_application(config=...)` now composes one finite delegation budget from
the host's frozen `[capacity]` configuration. A first-level workflow caller
must create a trusted `OperationContext` with a stable, host-issued
`correlation_id`, set both `LineageRecord.root_id` and `parent_id` to
`DelegationService.root_id_for_context(context)`, and pass that same context
through dispatch and workflow advance. The service registers the operation root
once in the canonical child store, bound to the context principal and budget.
Reusing that operation cannot mint a second root by changing its lineage label.
Durable children inherit all finite ancestry ceilings; changing the host
budget or principal for an existing root is refused. Legacy callers that
instantiate `DelegationService` directly without host root configuration own
their own root registration and cannot claim the production host grant.

To launch descendants, first call `integrate(parent_request, terminal_result)`;
then pass that same sealed successful result and a `NeedsDelegation` containing
host-reviewed `SpecialistRequest` values to `dispatch_proposal`. The proposal
digest must match the durable parent output. Each specialist carries a narrow
workspace, budget, preset and `WorkerExecutionContract`. Speculative siblings
must own the same logical question, no files, and distinct lane IDs plus SHA-256
digests of their normalized prompts. Direct fanout is at most three and
descendants stop after depth two; all children still pass canonical atomic
resource and ownership admission. If a later child cannot be admitted,
`PartialDelegationError.dispatched` preserves already admitted handles for
normal supervision. A repeated operation/proposal reuses existing durable
terminal children after restart.

After integrating all sibling results, `fan_in_hypotheses` reads their canonical
terminal records. It returns bounded hashes of output and proposed notes, not
child transcripts. It chooses a winner only when the trusted host supplies
complete `ArtifactReadiness` receipts and an independently recomputing
`verify_artifact(record, result) -> sha256` callback. The barrier binds every
receipt to the persisted child's output bytes and source prompt; partial,
stale or mismatched receipts fail the entire join. Without independent host
verification, the decision has no winner. This boundary verifies delegated
text output only; it does not attest workspace mutations or arbitrary external
files. The result's `mutations` field remains empty until a separate durable
host mutation attestation is available.
