# Fixed-peer outbound artifact mobility

**Date:** 2026-09-07
**Status:** Revised proposed design; no production implementation in this branch
**Base inspected:** origin/main at 7226959a275021c9d8bb4e792c780fbc2ebd7c8b

## Purpose

Add one explicit, operator-invoked outbound copy of one pre-admitted local
artifact to one fixed, independently permissioned peer. The local operation
gets a durable receipt, one owner, one in-flight dispatch lease, and an
explicitly invoked resume path after an interruption.

This is content transport. It does not migrate a job, model, memory authority,
scheduler lease, owner, epoch, node role, or runtime process. It does not
discover peers, rebalance resources, retry in the background, take over,
fail over, elect a leader, or change ownership.

## What the current code proves and does not prove

| Area | Evidence at the inspected base | Design consequence |
| --- | --- | --- |
| Receiver authority | ArtifactTransferBinding in sonder_runtime/bootstrap/artifact_transfer.py authenticates a receiver bearer and authorizes a TransferGrant from artifact_transfer configuration. | It is a receiver binding, not a generic local export authority. |
| Artifact scope | TransferGrant.scope_id is the hash of principal_id, project_id, and node_id. SQLiteArtifactTransferStore selects artifacts by that scope in artifact and read_range. | Changing a source receiver peer_node_id to the destination only exposes artifacts already written under that exact destination-specific scope. It cannot safely export arbitrary locally sealed artifacts. |
| HTTP composition | serve.configure_typed_config always constructs and starts ArtifactTransferBinding when artifact_transfer is enabled; HTTP handlers dispatch artifact routes to that binding. | Enabling a receiver to read local export bytes also exposes receiver HTTP routes. A source-only export path must not use that composition. |
| Receiver durability | The transfer store durably deduplicates by receiver scope plus command, resumes a verified offset, and seals a content-addressed artifact after full digest verification. | A fixed outbound operation can reuse one canonical remote command, but a local journal is still required for source-side intent, ownership, fencing, and recovery. |
| Existing peer client | HttpsArtifactTransferPeer disables proxies and redirects, bounds bodies, and uses a bearer credential. It accepts a ComputeNode and has no receiver identity attestation. | A destination label or compute node is not an authenticated artifact peer. The new peer path needs an independent certificate pin and recipient attestation. |
| Current wire receipt | Store receipts contain transfer ID, state, offset, chunk size, expiry, and sealed artifact metadata. Unsealed begin/inspect receipts do not echo immutable spec or command. | A mobility client cannot trust an existing begin/inspect response before bytes. A versioned authenticated envelope must echo the attested immutable contract. |
| Existing tests | The focused receiver/client suites passed on the inspected base: 51 passed in 30.65 seconds. | They establish the receiver mechanics, not source-only export, remote identity, leased source dispatch, or production outbound mobility. |

The unmerged codex/artifact-mobility-rehearsal branch is useful
process-boundary evidence only. Its test-only loopback HTTP path does not prove
certificate pinning, independent hosts, identity attestation, automatic
migration, or failover.

## Chosen boundary

The first production slice has four deliberately separate authorities:

1. ArtifactMobilitySourceBinding owns a private local export spool and a
   destination-independent source scope. It is never registered with the HTTP
   server.
2. ArtifactMobilityBinding owns the source-side journal, operation lease, and
   fixed-peer client. It is composed by the local application, not by an HTTP
   request handler.
3. The destination ArtifactTransferBinding remains the write authority. It
   independently grants the sending source owner access.
4. A narrow authenticated receiver protocol extension attests the destination
   grant and wraps existing begin/inspect receipts for the mobility client. It
   is not a local controller, scheduling, or model-command endpoint.

The local operator front door is:

    sonder-runtime artifact-mobility send --source-artifact ID --confirm-destination LABEL
    sonder-runtime artifact-mobility resume --operation-id ID
    sonder-runtime artifact-mobility status --operation-id ID
    sonder-runtime artifact-mobility list

Send creates the canonical operation ID itself and returns it. The caller
cannot select an operation ID, remote command, destination URL, credential,
source path, source scope, or recipient grant.

### Options considered

| Option | Result |
| --- | --- |
| Raw CLI directly calls ArtifactTransferClient | Rejected. It has no source-only authority, recipient identity fence, durable intent, lease, tombstone, or redacted source-side receipt. |
| Reuse ArtifactTransferBinding as the source with peer_node_id set to the destination | Rejected. Its storage is destination-grant-scoped and enabling it composes HTTP routes. It would both miss unrelated local artifacts and change receiver exposure. |
| Application-owned source binding plus mobility service and CLI | Chosen. It creates a stable local export namespace, durable source control, and a narrow operator boundary. |
| General HTTP or REPL mutation command | Deferred. It would need independent caller authorization, user approval, source admission, audit, and disclosure controls. |
| Treat compute.allow_remote or ComputeNode as consent | Rejected. Remote compute and artifact copy are separate permission lanes. |

## Source-only export authority

ArtifactMobilitySourceBinding is a new local-only bootstrap binding. It uses a
private spool distinct from artifact_transfer.store_dir and from every writable
workspace/home root. It is not passed to serve.configure_typed_config, has no
bearer authentication API, and no HTTP facade or route.

Its typed configuration is separate from both source and destination receiver
configuration:

    [artifact_mobility_source]
    enabled = true
    store_dir = "D:/sonder-private/export-spool"
    principal_id = "private-cluster"
    project_id = "sonder"
    source_owner_id = "node-a"
    max_object_bytes = 268435456
    total_bytes = 2147483648
    ttl_seconds = 86400

The binding derives a stable source scope from principal_id, project_id, and
source_owner_id. This source scope is never derived from destination_node_id,
destination origin, a receiver grant, a caller, or a transfer request. A
source artifact ID therefore identifies an artifact in one stable, private
export namespace and cannot cross a changed source scope.

The source spool uses the existing content-addressed transfer-store mechanics
behind a local-only adapter, but it does not reuse ArtifactTransferBinding or
its HTTP context. Its internal local grant is issuer-bound to the source
binding, cannot be forged from a CLI argument, and is limited to the one
derived source scope. The adapter supports inspect and bounded range reads for
mobility dispatch.

### Source admission

Outbound send accepts only an artifact already sealed in the source-only export
spool. It cannot read an arbitrary file, an arbitrary receiver-store artifact,
or an artifact from another export scope.

The first slice includes an injected, in-process source publisher port. A
trusted local workload/artifact producer that already holds the appropriate
application permission may write a stream into the export spool and receive a
sealed source artifact ID. This publisher port is not exposed through the
outbound CLI, REPL, MCP, or HTTP server. A general file-staging command is
outside this slice; adding one would need its own permission and provenance
design. Until an approved local publisher is wired for a workload, that
workload cannot use outbound mobility. The implementation must fail
unavailable rather than silently fall back to an ArtifactTransferBinding.

This split avoids two unsafe shortcuts:

1. a destination choice cannot alter which local artifacts are visible; and
2. enabling local export does not make an HTTP artifact receiver reachable.

## Destination configuration, credential, and identity

ArtifactMobilityConfig is separate from artifact_mobility_source and
artifact_transfer:

    [artifact_mobility]
    enabled = true
    destination_label = "node-b"
    destination_origin = "https://node-b.example.invalid:9443"
    destination_tls_certificate_sha256 = "64 lowercase hexadecimal characters"
    expected_recipient_attestation_sha256 = "64 lowercase hexadecimal characters"
    destination_credential_id = "node-b-transfer-key-v1"
    max_object_bytes = 268435456
    attempt_timeout_seconds = 30
    attempt_lease_seconds = 90
    receipt_ttl_seconds = 604800
    max_live_operations = 64

destination_label is display and confirmation text only. It is not a remote
identity assertion.

destination_tls_certificate_sha256 pins the exact DER leaf certificate hash
observed on the HTTPS connection. A production client verifies it during the
TLS handshake before it sends an HTTP request, bearer, or artifact byte. A
certificate rotation therefore requires an intentional configuration update and
a new outbound operation, rather than a silent resumed redirect.

expected_recipient_attestation_sha256 pins a canonical destination attestation
described below. It binds the destination receiver identity, principal,
project, authorized source owner, grant ID, grant revision, write permission,
and maximum object size. It is checked before begin and before every resumed
append sequence.

destination_credential_id is a nonsecret configuration generation. Operators
must change it whenever the destination receiver bearer changes. The journal
also stores a private keyed binding digest derived with the current bearer.
Either a changed generation or a changed digest makes an existing operation
terminal; a credential rotation never redirects or resumes a prior operation.
The digest is not emitted through a receipt, status response, log, or error.

Secrets.artifact_mobility_peer_key is supplied only through
SONDER_ARTIFACT_MOBILITY_PEER_KEY. It is the destination receiver bearer. It
must be 32 to 512 printable ASCII characters, reject control characters and
URL-looking credential forms, differ from api_key and artifact_transfer_key,
be forbidden in TOML, and redact to presence only. Validation and errors must
name the field and a stable code only; they must never reflect the provided
value, including a malformed URL-looking value.

The implementation validates:

1. all three sections are disabled by default;
2. every identifier is bounded printable text;
3. origin is absolute HTTPS with an explicit port and no non-root path, query,
   fragment, user info, or embedded credential;
4. the certificate and recipient-attestation pins are lower-case 64-hex;
5. source and mobility spool roots are private, disjoint, and outside writable
   workspace/home roots;
6. source max_object_bytes bounds the requested local artifact;
7. timeout and lease duration are bounded, and the lease exceeds one RPC plus
   renewal margin;
8. receipt retention and max_live_operations are bounded; and
9. mobility configuration never attempts to validate a destination's dynamic
   quota or limit locally.

The destination independently enables artifact_transfer with can_write true
and a valid write grant whose peer_node_id equals the configured
source_owner_id. It also configures a stable receiver_identity_id used in the
attestation. The destination receiver enforces its actual per-object size,
quota, capacity, expiry, and write policy during begin. A source-side
preflight can compare the attested maximum object size, but cannot promise
dynamic destination quota or capacity.

## Recipient attestation and versioned receiver envelope

The current receiver does not attest its node identity. The mobility slice
adds a narrowly authenticated versioned protocol to the existing receiver
surface. It is available only when the destination receiver grant is enabled;
it is not enabled by artifact_mobility_source or artifact_mobility on the
sender.

The recipient exposes an authenticated recipient-attestation response under
the existing artifact transport authentication and transport rules. Its
canonical content is:

    protocol_version
    receiver_identity_id
    principal_id
    project_id
    authorized_source_owner_id
    grant_id
    grant_revision
    can_write
    max_object_bytes

The canonical JSON SHA-256 is the recipient attestation pin. The response may
include only the canonical fields and its digest. It must not include the
bearer, receiver store path, quota usage, raw configuration, or exception
detail. TLS certificate pinning authenticates the endpoint serving the
attestation; the expected digest authenticates the configured logical receiver
and grant contract.

The existing begin and inspect HTTP operations gain a strict request-header
contract value for mobility-v1. Legacy clients retain their existing response
shape. With that contract value, begin and inspect return a bounded envelope:

    protocol_version
    recipient_attestation
    command_id
    spec
    receipt

The receiver obtains command_id and spec from its durable row, not from an
untrusted echo body. The mobility peer rejects an envelope unless:

1. the TLS leaf certificate hash equals the pinned hash;
2. recipient_attestation hashes to the configured expected value;
3. authorized_source_owner_id equals the local source owner;
4. can_write is true and the source spec is no larger than attested
   max_object_bytes;
5. command_id equals the local canonical command;
6. spec exactly equals the immutable local source spec; and
7. receipt has a valid transfer ID, bounded chunk size, state, and offset.

The client fetches and validates attestation first. It then sends begin and
validates the complete envelope before any append request. On resume it
validates a mobility-v1 inspect envelope before it appends a byte. A receiver
that changes the same origin, display label, bearer, grant, or receiver identity
cannot be accepted merely because its URL still resolves.

The final seal/inspect path must return an envelope with the same attestation
and immutable spec. The local journal marks sealed only when the sealed
artifact's digest, size, and media type exactly equal the source spec.

## Canonical operation and destination scope

The service, not the caller, generates operation_id as lower-case 128-bit
random hexadecimal. It derives:

    destination_scope = SHA-256 of the protocol version, source owner,
                        pinned certificate hash, and expected recipient attestation
    remote_command_id = mobility-v1.<destination-scope-prefix>.<operation-id>

The remote command is within the existing 128-character command grammar.
The receiver already deduplicates by its own grant scope plus command. The
locally derived destination scope prevents an operation from accidentally
sharing a command across a different intended receiver binding.

Every operation record has one immutable source_owner_id. The journal refuses
to open under another source owner or to dispatch an operation through another
source scope. A local OS/file ownership guard and a database compare-and-swap
lease ensure at most one dispatch owner acts on an operation at a time, even
if two local CLI processes race.

## Durable journal, leases, and lifecycle

ArtifactMobilityRepository is a private SQLite database distinct from both the
source spool and the destination transfers.sqlite database. It persists only
the fields necessary to resume safely:

| Immutable field | Purpose |
| --- | --- |
| operation_id and source_owner_id | Canonical single-owner identity. |
| source_artifact_id and exact spec | Fixed source bytes and integrity contract. |
| destination label for local display | Operator confirmation/display only. |
| destination binding HMAC | Private comparison value; no raw origin, certificate pin, attestation pin, or credential is stored. |
| destination credential generation | Detects deliberate key rotation. |
| destination scope and remote command | Stable remote idempotency fence. |
| created time and receipt expiry | Local lifecycle. |

Mutable fields are state, constrained outcome code, attempt epoch, lease
expiry, bounded attempt count, receiver transfer/artifact IDs, and timestamps.
They never contain a raw origin, source path, certificate pin, attestation
pin, credential, request/response body, payload byte, raw exception, or
peer-generated text.

The repository atomically writes the immutable operation before any request
that can mutate the receiver. It uses compare-and-swap operations:

1. acquire_dispatch atomically changes an eligible record to dispatching,
   creates a new opaque lease owner/token and monotonic attempt epoch, and
   records a short lease expiry;
2. renew_dispatch and every state update require the same operation ID, lease
   token, and attempt epoch;
3. a competing CLI gets BUSY and performs no network I/O;
4. a stale worker cannot update state after another owner acquires a later
   epoch; and
5. restart recovery changes only expired dispatching leases to resumable. It
   never contacts a peer during recovery.

The explicit state graph is:

    ready -> dispatching -> resumable
    ready/resumable/awaiting_seal/retryable_blocked -> dispatching
    dispatching -> awaiting_seal -> dispatching
    dispatching -> sealed
    dispatching/ready/resumable/awaiting_seal -> retryable_blocked
    dispatching/ready/resumable/awaiting_seal/retryable_blocked -> terminal_blocked
    dispatching/ready/resumable/awaiting_seal/retryable_blocked -> expired
    dispatching -> resumable after an expired lease is recovered locally
    sealed/terminal_blocked/expired -> receipt-pruned plus permanent tombstone

retryable_blocked means a temporary source or destination condition such as
receiver capacity, quota, or temporary availability prevented progress without
changing immutable identity. Only an explicit resume can leave it, and resume
must first revalidate the same source owner, source spec, certificate pin,
recipient attestation, destination binding HMAC, and credential generation.

terminal_blocked means an immutable or integrity fence failed: source scope
changed, source spec changed, destination binding HMAC changed, credential
generation changed, certificate pin changed, recipient attestation changed,
wrong remote command/spec, malformed protocol, or content mismatch. It cannot
resume and requires a new source operation after operator repair.

A completed, expired, or terminally blocked operation produces a compact
permanent tombstone keyed by the canonical source owner, destination scope, and
operation ID. Receipt details may be pruned at receipt_ttl_seconds, but the
tombstone is never evicted automatically. A new operation must always receive
a fresh generated ID; reuse is rejected even after receipt pruning. If the
journal cannot retain a tombstone, it fails creation rather than silently
reusing an identifier.

## Dispatch sequence

One explicit send or resume invocation performs at most one bounded attempt:

1. read current typed source and mobility config;
2. acquire the compare-and-swap dispatch lease;
3. inspect the sealed source artifact through the source-only binding and
   require its scope, exact spec, and configured source size limit;
4. compare the current private destination-binding HMAC and credential
   generation with the immutable operation, rejecting a mismatch terminally;
5. establish a direct HTTPS connection and verify its certificate hash before
   sending a bearer or request;
6. fetch and validate recipient attestation before a begin or append byte;
7. reject locally if attested max_object_bytes is smaller than source size;
8. call mobility-v1 begin or inspect and validate the complete immutable
   envelope before the first append;
9. stream bounded source ranges, renewing the lease and source admission
   between chunks, and validate every remote acknowledgement;
10. seal once, validate its envelope/final artifact, write sealed or a
    constrained nonterminal outcome with the same lease; and
11. release the lease locally. No timer, process, or scheduler resumes it.

The destination receiver independently admits begin. Dynamic destination quota,
active-transfer capacity, grant expiry, and per-object limits may change after
attestation. A begin rejection produces a constrained retryable or terminal
outcome according to its stable code; no artifact bytes have been sent.

## Composition, status, and user interfaces

ArtifactMobilitySourceBinding and ArtifactMobilityBinding are lazy
application-owned resources. They validate private paths and local state on
construction but do not open a network connection. Close cancels no remote
work; a persisted lease is recovered only after expiry by a later explicit
local command.

serve.configure_typed_config continues to compose ArtifactTransferBinding only
for artifact_transfer. It must not compose either source-only or outbound
binding merely because a server starts. The destination receiver's authenticated
attestation/envelope extension belongs to artifact transfer routing and follows
the current TLS, loopback, proxy, body-limit, logging, and authorization
policies. It is not an outbound-control endpoint.

The app and REPL may display redacted local status/list projections after the
service exists. They gain no transfer mutation command in this slice. The
outbound CLI returns source artifact ID, generated operation ID, destination
label, state, and constrained outcome code only. It never prints the origin,
certificate pin, recipient-attestation digest, binding HMAC, credential
generation, bearer, request body, or remote exception.

Operational capabilities add a fixed_peer_artifact_copy projection only when
the source-only binding and outbound configuration compose locally. Its reason
states that it is an operator-invoked fixed-peer copy with a pre-admitted local
source. automatic_artifact_migration remains unavailable.

## Guarantees after implementation

1. A source artifact comes only from a sealed, private,
   destination-independent source-only export scope.
2. Enabling that source scope does not add an HTTP artifact route.
3. A copy goes only to the configured HTTPS endpoint whose leaf certificate
   and recipient grant attestation match pins before upload bytes.
4. The destination independently authorizes the configured source owner for
   writes and independently enforces current size, quota, capacity, and expiry.
5. The local intent and immutable transport fence exist before receiver
   mutation, and one compare-and-swap lease permits only one dispatch owner.
6. Explicit resume reuses the canonical command only while every immutable
   source and destination fence still matches.
7. Credential/certificate/grant/receiver-identity changes make an existing
   operation terminal; they cannot silently redirect a resume.
8. A sealed local receipt has exact digest, size, and media-type agreement
   with the source specification.
9. Terminal IDs cannot be reused, even after detailed receipt retention ends.
10. Public receipts, status, logs, and errors contain only constrained codes
    and no endpoint, pin, credential, fingerprint, payload, path, or raw
    exception detail.

## Non-guarantees

This feature does not:

1. copy an arbitrary local file or arbitrary receiver-store artifact;
2. make a source store replicated or safe to destroy after a copy;
3. migrate jobs, models, memory, sessions, scheduler state, leases, owners,
   epochs, or node roles;
4. perform automatic retries, discovery, balancing, failover, takeover,
   failback, election, quorum, or witness voting;
5. prove a remote receiver's dynamic quota/capacity before begin;
6. override the destination's retention, quota, durability, or certificate
   operation policy;
7. create a generic remote artifact browser, cross-cluster sharing surface, or
   HTTP/REPL model mutation surface; or
8. claim that a loopback test proves independent-node TLS deployment,
   high availability, or cluster-level replication.

## Acceptance evidence required

The implementation must prove:

1. source-only config enabled with artifact_transfer disabled produces no
   artifact HTTP route, and source artifacts cannot cross a changed local
   source scope;
2. a source artifact must be sealed in the source-only spool, and a source
   receiver artifact or arbitrary path is not accepted by send;
3. same destination origin/label with a changed credential, certificate pin,
   receiver identity, grant, or attestation becomes terminal before append;
4. begin and inspect envelopes with wrong command, spec, or attestation are
   rejected before bytes;
5. a smaller attested max size rejects locally, while destination quota and
   capacity rejection remains receiver-enforced and byte-free;
6. two concurrent resumes produce one remote dispatch, stale lease writes
   fail, crash recovery makes no network call, and a recovered operation needs
   an explicit resume;
7. receipt pruning preserves a permanent tombstone and rejects operation-ID
   reuse;
8. malformed origin or URL-looking credential never appears in a serialized
   receipt, log, exception, or configuration error; and
9. a process-boundary loopback rehearsal is labeled as such, while a real
   independent-host pinned TLS test remains a deployment gate.
