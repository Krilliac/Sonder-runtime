# Plan: fixed-peer outbound artifact mobility

## Context

This plan implements the revised safety design in
docs/superpowers/specs/2026-09-07-outbound-artifact-mobility-design.md. It
creates a private source-only export scope, pins and attests the destination
before upload bytes, and gives each copy a single durable owner and leased
dispatch attempt.

The implementation must start from origin/main commit
7226959a275021c9d8bb4e792c780fbc2ebd7c8b or a reviewed descendant. The
existing receiver transfer service and client are useful mechanics, but they
are not a safe local export source or a remote identity protocol on their own.

## Global constraints

1. Keep artifact_mobility_source and artifact_mobility disabled by default.
2. Never use ArtifactTransferBinding as the outbound source authority and never
   derive source scope from a destination peer.
3. A source-only binding must never cause an HTTP route, listener, bearer
   authentication surface, or receiver grant to appear.
4. Never accept a caller-selected source path, source scope, destination URL,
   remote command, operation ID, grant, or bearer credential.
5. Treat destination_label as display text only. Authenticate a destination
   with a pinned TLS leaf certificate and a pinned recipient attestation before
   any artifact bytes.
6. Do not use compute.allow_remote or ComputeNode as artifact-copy consent.
7. Persist immutable local intent before a receiver mutation. Hold a
   nonblocking OS-level exclusive operation lock continuously across every peer
   call, then use a compare-and-swap lease as an additional fence.
8. A changed credential, certificate, attestation, grant identity, source
   scope, immutable spec, or destination binding is terminal for an existing
   operation. It requires a new operation; never redirect a resume.
9. Do not locally claim to know destination quota/capacity. Compare only an
   attested static object-size limit, then let the receiver independently
   admit or reject begin.
10. Never log, serialize, or surface an origin, certificate pin, attestation
    pin, binding HMAC, credential, URL-looking malformed credential, payload,
    source path, raw response, or raw exception.
11. Do not add automatic retry, migration, scheduling, failover, takeover,
    owner/epoch change, discovery, balancing, or a model-facing mutation
    endpoint.
12. Work only in an isolated worktree. Use targeted git adds, DCO sign-off, and
    do not modify an installed runtime.

## Task 1: add separate typed configuration and non-disclosing secrets

Files:

    sonder_runtime/platform/config.py
    sonder_runtime/platform/artifact_mobility_config.py
    sonder_runtime/platform/artifact_mobility_source_config.py
    sonder_runtime/platform/artifact_transfer_config.py
    tests/test_artifact_mobility_config.py
    tests/test_artifact_mobility_source_config.py
    tests/production/test_artifact_transfer_binding.py

Add ArtifactMobilitySourceConfig with enabled, private store_dir, principal_id,
project_id, source_owner_id, max_object_bytes, total_bytes, and ttl_seconds.
Its derived source scope is canonical principal/project/source owner and does
not contain a destination field.

Add ArtifactMobilityConfig with enabled, destination_label, destination_origin,
destination_tls_certificate_sha256, expected_recipient_attestation_sha256,
destination_credential_id, max_object_bytes, attempt_timeout_seconds,
attempt_lease_seconds, receipt_ttl_seconds, and max_live_operations.

Add receiver_identity_id to ArtifactTransferConfig. A receiver without that
stable identity must reject the mobility-v1 contract while preserving legacy
artifact transfer behavior.

Add Secrets.artifact_mobility_peer_key from
SONDER_ARTIFACT_MOBILITY_PEER_KEY only. It is TOML-forbidden and
presence-redacted. Reject control characters and URL-looking forms without
placing the rejected string in ConfigError, repr, diagnostics, or logs. Require
the key to differ from api_key and artifact_transfer_key.

Validate all private source paths against state workspace/home roots and against
each other. Validate destination origin without echoing it: strict HTTPS,
explicit port, root-only path, no query, fragment, user info, or embedded
credential. Validate both pins as lower-case 64-hex. Validate the source object
limit locally; do not require it to be no larger than an unknown destination
limit. Bound timeouts, leases, retention, and live-operation counts.

Tests:

1. disabled defaults for every new section;
2. source owner/source-scope canonical stability across destination changes;
3. invalid origin forms, including user-info values that look like credentials,
   report only a stable field/code;
4. secret source, redaction, TOML rejection, key distinctness, and malformed
   URL-looking secret non-disclosure;
5. path overlap and source/destination storage separation;
6. receiver identity required only for mobility-v1;
7. valid compute remote configuration grants no mobility permission.

## Task 2: implement the local-only source authority and publisher port

Files:

    sonder_runtime/bootstrap/artifact_mobility_source.py
    sonder_runtime/application/artifacts/mobility_source.py
    sonder_runtime/application/ports/artifact_mobility.py
    sonder_runtime/adapters/persistence/artifact_mobility_source.py
    sonder_runtime/application/services.py
    tests/test_artifact_mobility_source.py
    tests/test_artifact_mobility_source_http_isolation.py

Create ArtifactMobilitySourceBinding around a private source spool and an
issuer-bound internal context. It can reuse the low-level sealed
content-addressed storage contract, but it must not import or subclass
ArtifactTransferBinding, accept a bearer, or use artifact_transfer
configuration.

Expose only injected in-process publisher and reader ports:

    publish_sealed(stream, immutable_spec, trusted_provenance)
    inspect_sealed(source_artifact_id)
    read_range(source_artifact_id, offset, length)

The publisher must be callable only through an application-owned capability
object held by a trusted local workload/artifact producer. Do not add a file
path argument to send, a generic stage CLI command, an MCP method, REPL method,
or an HTTP handler. If no trusted publisher is wired, outbound send is
unavailable.

The source reader must enforce the one derived scope and source limits on every
inspect/range read. A different source owner or source scope cannot inspect or
read an existing source artifact.

Tests:

1. with artifact_mobility_source enabled and artifact_transfer disabled,
   configure_typed_config does not create an artifact HTTP receiver and all
   artifact routes return the normal not-found result;
2. source-only construction does not bind a port or call a network client;
3. a source artifact published under source owner A is unreadable under owner B
   or a changed principal/project scope;
4. a receiver-store artifact ID and arbitrary filesystem path cannot enter the
   mobility send request;
5. close/reopen preserves source artifacts within the same source scope;
6. private-root overlap and issuer-forgery attempts fail closed.

## Task 3: add recipient identity attestation and mobility-v1 envelopes

Files:

    sonder_runtime/bootstrap/artifact_transfer.py
    sonder_runtime/application/artifacts/transfer.py
    sonder_runtime/adapters/persistence/artifact_transfer.py
    sonder_runtime/interfaces/http/artifact_transfer.py
    sonder_runtime/interfaces/http/facades/artifact_transfer.py
    sonder_runtime/interfaces/http/serve.py
    tests/test_artifact_transfer_mobility_protocol.py
    tests/test_artifact_transfer_production_http.py

Add one authenticated recipient-attestation control response under the existing
artifact receiver routing/authentication/transport policy. Its canonical
content is protocol version, receiver_identity_id, principal_id, project_id,
authorized_source_owner_id, grant_id, grant_revision, can_write, and
max_object_bytes. Return its canonical SHA-256 and no secret, store path, quota
usage, raw config, or error detail.

Add an exact mobility-v1 request-header contract to begin. Legacy requests and
response shapes remain unchanged. A mobility-v1 response envelope must be
assembled from the receiver's durable upload row and current authorized
receiver binding:

    protocol_version
    recipient_attestation
    command_id
    spec
    receipt

The receiver must not manufacture spec or command from a caller-provided echo.
It must reject mobility-v1 before row creation/deduplication when
receiver_identity_id is missing or invalid, with a stable no-detail result;
this leaves legacy begin behavior unchanged. The begin envelope endpoint uses
the same authenticated binding, TLS/loopback
checks, body caps, no-proxy trust policy, and redacted logging behavior as
artifact transfer. It is a destination receiver protocol extension, not an
outbound controller route.

Add a separate mobility-v1 receipt-inspection action for resume/final
confirmation. Legacy general upload inspection remains can_read-only. On
operation creation, the sender generates a 256-bit receipt capability and
persists it only as protected private journal data encrypted with a key derived
from the current mobility peer credential and operation ID. Begin transmits
the capability only over pinned TLS; the receiver stores a keyed verifier
bound to receiver scope, transfer ID, and durable command. Resume/final
inspection requires the authenticated bearer, current can_write grant, exact
transfer ID/command, contract header, and matching receipt capability.

First mobility-v1 begin atomically stores the verifier with the immutable
durable row. A replay of that same mobility-v1 command must verify the incoming
capability against the original stored verifier before building an envelope; it
must not replace the verifier/capability binding, immutable command/spec, or
receipt state. A missing or wrong capability receives only a stable
authorization/protocol result, with no envelope or receipt data.

Header absence is a strict legacy request. A legacy request that resolves to a
mobility-v1 row must be rejected before it can return or mutate its protected
mobility receipt/envelope, and it cannot downgrade that row. A mobility-v1
request resolving to a legacy row without a verifier must also be rejected; it
cannot retrofit a verifier/capability into legacy state. Both cases require a
fresh canonical operation where mobility-v1 is needed.

The receipt action returns only the bounded mobility envelope for that exact
row. It may operate for can_write true/can_read false, but it must not permit
artifact bytes, artifact metadata, arbitrary upload inspection, upload listing,
or a general read grant. Do not use the normal read authorizer as a fallback.

Tests:

1. unauthenticated or disabled receiver cannot retrieve an attestation;
2. the canonical attestation changes when receiver identity, source owner,
   grant ID/revision, write permission, or max object size changes;
3. legacy begin/general-inspect responses remain compatible and read-gated,
   while a missing receiver_identity_id rejects mobility-v1 before row creation
   or envelope construction without changing legacy behavior;
4. mobility-v1 begin and transfer-bound receipt inspection echo the durable
   command and exact durable spec, including a resumed record;
5. a replay verifies the original stored verifier/capability, rejects a
   missing/wrong capability without returning an envelope or replacing the
   verifier, and preserves the immutable durable row;
6. header-absent legacy requests cannot read, return, mutate, or downgrade a
   mobility-v1 receipt/envelope, while a mobility-v1 request cannot install a
   verifier onto a legacy row;
7. malformed/missing version header and malformed envelope fail with stable
   codes and do not reveal configuration;
8. a can_write true/can_read false receiver resumes and confirms only its own
   transfer with the right receipt capability, while legacy inspection,
   artifact metadata, byte-range reads, and wrong-transfer capability attempts
   remain forbidden; and
9. the endpoint cannot be enabled by source-only configuration on a sender.

## Task 4: add a pinned fixed-peer mobility client

Files:

    sonder_runtime/adapters/compute_fabric/artifact_mobility.py
    sonder_runtime/adapters/compute_fabric/http_client.py
    sonder_runtime/application/ports/artifact_mobility.py
    tests/test_artifact_mobility_peer.py

Create a dedicated ConfiguredArtifactMobilityPeer. It receives typed mobility
configuration and a credential provider, not a ComputeNode. Use a direct HTTPS
connection with no proxy and no redirect. Obtain the peer DER certificate
during the TLS handshake and reject it unless its SHA-256 matches the configured
leaf-certificate pin before sending an HTTP request or bearer. Do not fall back
to platform CA trust alone.

The peer first fetches recipient attestation and compares its canonical digest
and fields with configuration/source_owner_id. It rejects a recipient with no
write permission or a smaller attested max_object_bytes before begin. It then
calls mobility-v1 begin or transfer-bound receipt inspection and validates
exact protocol version, attestation, command, immutable spec, transfer ID,
offset, chunk size, and state before any append. Seal/final confirmation uses
the same receipt capability and envelope checks.

The client preserves existing body caps, response-length verification,
chunk-digest validation, proxy disabling, redirect refusal, and no raw network
exception propagation. It must reject a credential that is absent, malformed,
contains a line break, or looks like a URL with a stable code and no echo.

Tests:

1. production HTTP, credentials in URL, redirects, proxies, incorrect leaf
   certificate, mismatched attestation, wrong source owner, and unknown
   protocol version fail before begin/append;
2. a same-origin/same-label peer with a changed receiver identity/grant
   attestation fails before append;
3. begin/receipt-inspection wrong spec, command, transfer ID, offset, chunk
   size, or capability fails before append;
4. source size above attested maximum fails locally before begin;
5. dynamic receiver QUOTA/CAPACITY/FORBIDDEN begin responses send no chunks and
   map only to constrained codes;
6. malformed credential URL text does not occur in returned error, repr, log,
   or serialized result.

## Task 5: implement the private journal, permanent tombstones, and leased CAS

Files:

    sonder_runtime/adapters/persistence/artifact_mobility.py
    sonder_runtime/application/artifacts/mobility.py
    sonder_runtime/application/ports/artifact_mobility.py
    tests/test_artifact_mobility_persistence.py

Create a source-owner-bound private SQLite journal. Persist immutable operation
ID, source owner/scope, source artifact/spec, destination display label,
destination scope, canonical remote command, credential generation, a private
HMAC of the complete destination binding, and receipt lifecycle timestamps.
Do not persist raw origin, pin, attestation pin, credential, source path,
payload, exception text, or raw peer response.

Generate operation ID inside the service as 32 lower-case hex and derive the
remote command from protocol version, destination scope prefix, and operation
ID. The caller cannot provide either. Persist a compact permanent tombstone
keyed by source owner plus destination scope plus operation ID when an operation
becomes sealed, expired, or terminally blocked. Detailed receipts may expire,
but tombstones never auto-delete. Fail creation instead of evicting a tombstone.

Implement these atomic repository operations:

    create_operation
    acquire_dispatch
    renew_dispatch
    transition_with_lease
    recover_expired_leases
    prune_receipt_keep_tombstone

Create a per-operation private lock file and use the platform's nonblocking
OS-level exclusive locking primitive; never substitute a sentinel file or
threading lock. Attempt admission is: acquire that lock nonblockingly, then
acquire_dispatch while continuously holding the lock. Keep the handle open
until all peer calls and lease-protected state transitions finish.

acquire_dispatch performs a compare-and-swap on state and lease expiry,
increments an attempt epoch, and writes a random opaque lease token. Every
renewal and transition predicates on operation ID, attempt epoch, lease token,
and an unexpired lease. A second process receives BUSY with no peer call. An
expired dispatching lease recovers to resumable only; recovery never performs
network I/O. A recovered attempt cannot run while a paused prior process still
owns the OS lock. The old process must check the lease immediately before each
peer call; after expiry it cannot revive its lease and must release the lock
without another peer call.

Use this exact public lifecycle:

    ready -> dispatching -> resumable
    ready/resumable/awaiting_seal/retryable_blocked -> dispatching
    dispatching -> awaiting_seal -> dispatching
    dispatching -> sealed
    dispatching/ready/resumable/awaiting_seal -> retryable_blocked
    dispatching/ready/resumable/awaiting_seal/retryable_blocked -> terminal_blocked
    dispatching/ready/resumable/awaiting_seal/retryable_blocked -> expired
    sealed/terminal_blocked/expired -> pruned receipt plus permanent tombstone

Only explicit resume acquires a new lease from resumable, awaiting_seal, or
retryable_blocked. Credential/binding/identity/spec failures use
terminal_blocked. Temporary source availability and receiver quota/capacity may
use retryable_blocked. There is no automatic dispatcher.

Tests:

1. immutable intent exists before a fake peer can observe begin;
2. two concurrent callers acquire one OS lock/lease and produce one peer
   dispatch;
3. stale token/epoch cannot renew or transition after lease replacement;
4. a crash/reopen only recovers expired leases and makes no peer call;
5. a paused sender holds the OS lock while its lease expires; a replacement
   resume returns BUSY with zero peer calls, the paused sender observes lease
   expiry before its next peer call and stops, then the replacement acquires a
   fresh lock/lease and is the only sender allowed to call the peer;
6. receipt expiry/tombstone pruning retains the no-reuse guard;
7. direct repository reuse of the canonical operation key fails after terminal
   receipt pruning;
8. changed source owner/scope, credential generation, or destination-binding
   HMAC is terminal and cannot resume;
9. raw origin, pins, HMAC, credential, payload, and exception sentinel are
   absent from rows intended for external projection and all serialized output.

## Task 6: implement one bounded dispatch service

Files:

    sonder_runtime/application/artifacts/mobility.py
    sonder_runtime/application/ports/artifact_mobility.py
    tests/test_artifact_mobility_service.py

Implement send and resume around the source-only reader, pinned peer, private
OS lock, and leased repository. Send validates local confirmation and a sealed
source artifact, creates immutable intent, then nonblockingly acquires the
operation lock and a lease. Resume reloads the single owner record and acquires
a new lock/lease only from an eligible nonterminal state.

For every attempt:

1. nonblockingly acquire and continuously retain the OS operation lock;
2. acquire a fresh current journal lease while holding that lock;
3. revalidate source binding/scope/spec;
4. compare current credential generation and private destination-binding HMAC;
5. verify TLS certificate and recipient attestation;
6. compare the attested static maximum size;
7. validate begin/transfer-bound receipt immutable envelope before append;
8. immediately before every peer call, prove the lock is held and renew an
   unexpired lease;
9. verify every acknowledgement and final sealed artifact receipt; and
10. use one lease-guarded constrained transition on completion/failure before
    releasing the lock.

A lost response becomes resumable only after its lease is released or expires.
It does not cause a background retry. A receiver verifying state becomes
awaiting_seal. A current source/destination availability issue is
retryable_blocked. An immutable fence or integrity failure is terminal_blocked.
A terminal record cannot resume.

Tests:

1. one stable canonical command is used after a simulated lost response;
2. resume validates recipient attestation and immutable transfer-bound receipt
   envelope before the first post-restart append;
3. same origin/label with changed peer key makes the binding HMAC mismatch
   terminal before a peer connection;
4. same origin/label with changed remote grant/receiver identity makes
   attestation mismatch terminal before append;
5. source revocation/scope change between chunks stops the operation;
6. a paused sender cannot overlap a replacement sender after its journal lease
   expires because the replacement cannot take the retained OS lock and the
   paused sender stops at its next pre-peer-call lease check;
7. no timer, worker, polling loop, failover, ownership mutation, or background
   network action occurs after a method returns.

## Task 7: compose lifecycle, capability projection, and narrow CLI

Files:

    sonder_runtime/bootstrap/artifact_mobility.py
    sonder_runtime/application/services.py
    sonder_runtime/__main__.py
    sonder_runtime/domain/operational_capabilities.py
    sonder_runtime/interfaces/repl/repl.py
    tests/test_artifact_mobility_binding.py
    tests/test_artifact_mobility_cli.py
    tests/test_operational_capabilities.py

Compose source-only and outbound bindings lazily through Application. They must
not be constructed by HTTP server startup and must not contact a peer during
construction, status, capability projection, list, or close. Close stops local
resources only and leaves an unexpired operation lease for later local recovery.

Add only these local CLI commands:

    artifact-mobility send --source-artifact ID --confirm-destination LABEL
    artifact-mobility resume --operation-id ID
    artifact-mobility status --operation-id ID
    artifact-mobility list

Send generates operation ID internally. Output exposes only operation ID, source
artifact ID, destination label, state, timestamps, and constrained outcome
code. Do not print origin, pins, binding HMAC, credential generation, or
peer-generated message. The REPL and app may display the same read-only
projection; neither gets a mutation command. Do not add a source file staging
CLI.

Add fixed_peer_artifact_copy to operational capabilities only when local source
and outbound config compose. Its reason says it requires a pre-admitted
source-only artifact and is operator-invoked. Preserve
automatic_artifact_migration as unavailable.

Tests:

1. source-only enabled with receiver disabled adds no artifact routes;
2. construction/status/list/capability/close make no peer call;
3. confirmation mismatch and arbitrary operation ID injection fail;
4. CLI/REPL/app projection redacts all private destination fields;
5. HTTP has no local outbound-controller route and no mutation route for the
   source publisher;
6. automatic artifact migration capability remains false.

## Task 8: composed evidence, docs, and full verification

Files:

    tests/test_artifact_mobility_composed.py
    docs/operations/artifact-mobility.md
    docs/operations/configuration.md
    docs/reference/runtime-capabilities.md
    docs/superpowers/specs/2026-09-07-outbound-artifact-mobility-design.md

Compose a source-only binding, source publisher, mobility journal, and two
receiver processes. A test-only numeric-loopback peer may prove durable remote
offset resume only through an explicitly injected test factory. Publish a
binary source artifact larger than one chunk, interrupt after a durable remote
offset, reopen source/journal, recover its expired lease locally, explicitly
resume, and prove exact destination seal. The test must state that it does not
prove real TLS certificate pinning or independent-host availability.

Add a separate deployed two-host acceptance test plan: real HTTPS endpoint,
leaf-certificate pin, recipient attestation pin, source-owner match, key/grant
rotation rejection before append, recipient quota rejection with no bytes, and
manual explicit resume. Record that evidence by exact revision and host
configuration fingerprint only; do not record secrets or endpoint values.

Run at minimum:

    python -m pytest -q tests/test_artifact_mobility_config.py tests/test_artifact_mobility_source_config.py tests/test_artifact_mobility_source.py tests/test_artifact_mobility_source_http_isolation.py tests/test_artifact_transfer_mobility_protocol.py tests/test_artifact_mobility_peer.py tests/test_artifact_mobility_persistence.py tests/test_artifact_mobility_service.py tests/test_artifact_mobility_binding.py tests/test_artifact_mobility_cli.py tests/test_artifact_mobility_composed.py
    python -m pytest -q tests/test_artifact_transfer.py tests/test_artifact_transfer_http.py tests/test_artifact_transfer_production_http.py tests/test_artifact_transfer_composed_streaming.py
    python scripts/check_architecture.py
    python scripts/check_requirement_evidence.py
    python scripts/check_error_signals.py
    python scripts/check_history_privacy.py --json
    python scripts/check_doc_links.py
    python scripts/generate_documentation_catalogs.py --check

Run the repository-required full test command before merge. Record exact
revision and results. Do not replace the live two-host pinned-TLS acceptance
gate with the loopback rehearsal.

## Review rejection conditions

Reject an implementation if it:

1. points outbound send at an ArtifactTransferBinding store or changes its
   receiver peer scope to select source artifacts;
2. enables an HTTP receiver because only a source-only spool is enabled;
3. identifies a destination only by a display label, DNS origin, or bearer;
4. appends bytes before certificate, recipient attestation, begin/receipt
   command, capability, and immutable spec checks;
5. treats a static source cap as proof of dynamic destination quota/capacity;
6. permits a changed key, grant, receiver identity, source scope, destination
   binding, or certificate to resume an old operation;
7. lets two dispatch attempts overlap peer calls, lets a paused expired-lease
   sender resume peer calls, or lets stale lease work update its state;
8. discards a terminal operation ID instead of retaining its tombstone;
9. exposes any private endpoint/fingerprint/secret/error/payload data; or
10. gives a write-only receiver general inspection or artifact-byte access; or
11. claims automatic migration, failover, ownership transition, or a live
    independent-host TLS proof that has not been executed.
