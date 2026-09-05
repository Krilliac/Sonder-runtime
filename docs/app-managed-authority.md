# Private managed app authority

This composition extends the existing host continuation service with a private authenticated app selection. It adds no HTTP route, account work admission, automatic restart, or public capability. Ordinary account lane execution remains refused unless this private authority is explicitly composed and the exact root has a live registered attachment.

## Host composition

Construct one `AppManagedAuthority(binding, lanes, capacity=128)` after the reviewed `AppControlBinding.start()` and lane service initialize the same private fleet database. Then call `binding.issue_selection(account_token=..., control_token=..., context=...)` from the trusted authenticated HTTP host. Raw bearers are consumed at issuance and are not retained in the selection. The immutable selection contains exact account reference, control/binding/selection records, grant/catalog identity, original context, and a nonserializable issuer.

Create a fresh service with `authority.continuation_service(selection, projection_codec=..., command_codec=..., terminal_result_codec=...)`. The host may pass this service to `ManagedStandaloneSession` or the existing explicit recovery coordinator. Codecs remain host-owned and unavailable by default. The host supplies the selection's narrowed `context` and exact `host_conversation_id`; public request IDs, chat aliases, and stored principals do not construct authority.

`LaneContinuationService.open_parent(context)` uses the private constructor-issued selection and mints a parent in the same authorized fleet transaction. Failed managed construction calls `discard_parent` only for its exact private mint. Cleanup failure retains a bounded private `PARENT_REVOCATION_UNCONFIRMED` entry rather than claiming revocation. This is not a credential vault. A successful registration removes the mint reference and installs a bounded process-local parent/owner/epoch registration. Exact close removes that generation before closing its owner lease.

## Transaction and effect rules

`service._transaction(context)` acquires the exact account source guard before the fleet writer. Its ephemeral admission is carried on the actual `LaneTransaction`; every account authorizer receives `connection=tx.conn`. Borrowing validates the app store's existing database identity and neither commits nor closes the caller's connection. Admission is tied to issuer, thread, exact context and first active connection. Root context variables carry an existing private bound handle only; they never carry or infer a connection.

`require_root_admission` checks the same live registration and continuation epoch inside the command transaction. Continuation selection, reattachment, pending-verification links and result commits use this helper. Verifier transactions use `root_transaction(store, context)`, which enters the bound service's pre-transaction hook. Existing local-owner authorizers retain their prior live context and behavior.

Background workers need a retained exact dispatch context and a separately service-issued worker proof. Copied HTTP/worker contexts do not start work. Worker cancellation is the existing lane/attempt token; workspace, tools, flags and finite deadline stay within the original account/control/binding/catalog ceiling. Parent registration is never reconstructed after restart. Clearing selection or account/key/catalog revocation blocks later admission. Provider/tool execution and approval callbacks run outside account/fleet locks, with a fresh check immediately before adapter invocation. Actual returned response/receipt evidence remains durable after revocation; no cleanup or replay is inferred.

The registry defaults to 128 selections/parents (maximum configured capacity 256), worker proofs are bounded at 512, retained dispatch contexts at 256, and private mint cleanup failures at 32. No expiry renewal, timeout ownership stealing, nested authority, remote execution expansion, distributed lease or cross-database atomicity is claimed. Shared account mutation guards cover the existing owned account API in this single host; external database writers are outside that guarantee.

## Integration boundary

The public managed-work orchestration and the separate host-turn transaction wiring are supplied by the host integration. This module does not enable those routes or replace their exact work approval, terminal evidence, output publication, transport or lifecycle checks. Recovery stays explicit and retains original projection/prepared identity. Missing registry/issuer/codec or an expired original ceiling refuses admission.
