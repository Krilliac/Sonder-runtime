# Private app-control persistence

This adapter stores bounded, immutable app-control metadata in the existing private fleet SQLite database. It supplies no authentication, HTTP route, model tool, credential generator, or attachment authority. Trusted host composition must validate the actual account session, current role, catalog and transport before and after each admission; the database cannot make those separate account-store checks atomic.

`SQLiteAppControlStore(path, *, limits=AppControlLimits(), clock=time.time)` requires the canonical existing fleet database. It initializes its additive schema outside authority transactions. `atomic(callback, *, connection=None)` uses BEGIN IMMEDIATE and FULL synchronous commit for an owned connection. A supplied connection must already own a transaction on the exact same database, with foreign keys and FULL durability enabled. Borrowing never commits, rolls back, or closes it. The callback transaction object expires on return. Temporary schema shadowing and changed database identity are refused.

The application port defines frozen GrantSnapshot, ControlSessionRecord, BindingRecord, SelectionRecord, CommandKey, CommandReceipt, CommandRecord, BindingPage and AppControlLimits values. Collections are tuples; identifiers, principal/account-reference shapes, canonical roots, byte bounds, timestamps and exact scalar types are validated. Account references and salt/verifier fields are private metadata, not public receipt fields. No raw account token, password or control secret belongs in this store.

The transaction API provides command, commit_enrollment, create_binding, select_binding, clear_selection, revoke_binding, read_session, read_binding, read_selection, require_selection and list_bindings. Mutation arguments are keyword-only after the CommandKey and use the exact signatures in the adapter. Limits are fixed on the store, never supplied by request data. The initial expected selection epoch is 0. Select/clear use compare-and-swap epochs; revoke fences every matching selection in the same transaction. Read methods return immutable private records and are not public JSON serializers.

Each command is scoped by principal plus exact private account-session reference for enrollment, or control-session ID for other actions. Its action and canonical argument digest are immutable. Mutation and fixed public receipt commit together, without a prepared pseudo-stage. Identical retry returns the original receipt; changed arguments conflict. Every committed enrollment retry must be translated by the host to CREDENTIAL_DELIVERY_UNKNOWN, never a new secret. A commit or post-commit validation failure raises OutcomeUnknown; reconcile the exact command before deciding another action. A borrowed callback result is provisional until its caller commits.

A session expires no later than min(account_expires_at, grant.expires_at, issued_at + session TTL). A binding expires no later than min(original account_expires_at, original grant.expires_at, created_at + binding TTL). It can outlive its short control session. A fresh live control session can select an existing binding only under the exact same immutable grant, without rewriting its original expiry. Grant revision high-water marks are persistent per runtime/grant ID. Rollback, same-revision payload changes, and same-revision catalog-source replacement are refused; current older grants are fenced.

All retained rows count toward finite per-account/global quotas, including revoked rows. Commands have a global cap and pages have count and byte bounds. There is no eviction or deletion policy. Exhaustion requires explicit operator handling; this slice does not claim indefinite retention capacity, cross-database atomicity, distributed ownership or filesystem isolation. Host composition must keep the fleet database and sidecars outside every model-writable root and serialize its live authority mutations appropriately.


## Borrowed-connection schema and mutation checks

The store rejects temporary objects shadowing protected table names, including
case variants, and rejects persistent or temporary triggers targeting any of its
six protected tables regardless of trigger name. These checks run before the
callback, around each write and at the transaction boundary. Arbitrary triggers
are not part of the app-control schema contract.

Each statement must affect exactly one row without extra changes, and the typed
entity or grant high-water is read back before publishing its command receipt.
The receipt itself is also read back exactly. Thus an ignored insert or ignored
updated column cannot become an acknowledged enrollment/revocation. A borrowed
connection remains owned by its caller on refusal; the caller must roll back its
transaction. The adapter never commits or rolls it back on the caller's behalf.

Binding revocation validates every decoded selection's principal and session
against its indexed row columns before changing the binding or any selection.
Corrupt scope refuses instead of propagating a record into another selection
slot. No row or receipt repair is attempted implicitly.
