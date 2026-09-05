# Scoped resumable artifact transfer

This package supplies a private receiver store, a typed service, a pure HTTP
facade and an HTTPS streaming client. It does **not** register production routes
or configure credentials. Host composition must provide authentication, a fixed
credential-to-grant mapping, HTTP bounds and request admission.

## Host authority

`ArtifactTransferService(store, authorizer=..., limits=TransferLimits())` requires
an authorizer callable `(OperationContext, action) -> TransferGrant`, with action
`read` or `write`. No authorizer means unavailable. Grants are frozen host data:
principal, project, authorized peer node, grant identity/revision/expiry,
read/write masks, object limit and scope byte quota. The service also checks
that the grant principal equals the authenticated context principal and that
the requested mask/expiry permits the operation.

`TransferGrant.node_id` names the authorized peer binding. It is not a claim
about the receiver machine's identity. Local receiver identity and credential
selection belong to host composition. Request bodies cannot supply principals,
projects, peers or grant overrides. Peer origins come from configured ComputeNode
objects and require HTTPS with normal certificate verification; credentials come
from a host callback and are not written into transfer metadata.

## Storage and limits

`SQLiteArtifactTransferStore(root)` creates a private anchored spool and a SQLite
ledger. Root must be outside **all** model-writable roots, including runtime
homes that ordinary file tools can access. A store ancestor of a writable root
is also rejected. Existing private spool primitives check ownership, permissions,
symlinks/junctions and opened-directory identity. It is not safe to exempt a
runtime state directory merely because its name sounds private.

Default chunks are 1 MiB; host chunk configuration is 64 KiB–1 MiB. All chunks
except the final one have the declared chunk size, bounding metadata amplification.
At most 65,536 chunks may be configured per object. Default object maximum is
256 MiB; trusted host/grant configuration can increase it up to 64 GiB. Host total
reservation default is 2 GiB; grants additionally bound per-scope byte use.
Active upload defaults are four per scope/eight overall. The ledger admits at
most 4,096 retained transfer records, including zero-byte/aborted transfers.
Metadata retention/compaction is a future host policy, not unlimited admission.

Before upload, reserve twice the declared payload size for staged bytes plus
verification/publication output. This includes a temporary verification copy
and unacknowledged chunk-publication gaps. Reservation decreases to published
size only after staging cleanup actually succeeds. Failed cleanup retains it.
These quotas count payload bytes; finite record/chunk ceilings bound metadata
growth independently. Filesystem overhead and configured disk headroom still
need operator capacity planning.

An OS lock serializes store mutations across processes and fails with BUSY on
contention. It also prevents concurrent verifiers from multiplying disk
temporaries. Verification uses bounded background workers (two process-wide)
and streaming reads. Chunk append is atomic across durable chunk publication and
the SQLite acknowledgement boundary; an orphan chunk is reconciled on exact
retry. Exact duplicate chunks are re-read and hashed before replaying their
stored receipt. After sealing, the corresponding published range is verified.
Aborted/failed transfers cannot replay an old successful append acknowledgement.

Published leaf filenames are SHA-256 digests within private principal/project/
peer and transfer namespaces. References contain an opaque artifact ID, SHA-256,
size and media type. There is no global digest-existence API, cross-scope
deduplication or sharing permission inferred from content equality. Cache reuse
is through the stable scoped transfer/command identity; distinct transfers may
retain separate copies.

## Lifecycle and retry semantics

Begin commands are scoped by principal/project/peer plus command ID. A changed
spec under that identity conflicts. Chunk receipts are keyed by transfer and
offset. Seal and abort command IDs are scoped by transfer and action: exact
retry is idempotent; a changed ID for that already admitted action conflicts.
They are not a single global command-ID namespace.

States are open, verifying, sealed, failed and aborted. Sealing returns a bounded
202-style verifying receipt; poll inspect for completion. An interrupted
verification can be resubmitted with its original seal command. Verification
checks original grant identity/revision/expiry and live authorization during
chunk processing, during recovery scans, after scans and before publication
receipt commitment. It never turns a fresh grant into an extension of an
existing upload's authority.

Known integrity failures become failed. Interrupted verification or transient
I/O failures leave verifying state so the caller can retry the same command;
inspect does not automatically resubmit after a process restart. No control
receipt promises bytes that have not passed size/digest verification.

The trusted local `store.reap_expired(limit=8)` maintenance API cleans only
expired open/verifying/failed staging. It cannot delete sealed objects. Abort
also deletes only receiver-owned staging. Published objects and receipts remain
pinned; there is no completed-object deletion/eviction API. This avoids evicting
bytes referenced by a live job, but general job/deployment reference tracking,
retention deletion and replica propagation remain future work.

## Wire and streaming client

The pure facade is `dispatch_artifact_transfer(service, action, payload, context,
body=b"")`. It returns `ArtifactTransferHttpResult(status_code, body)`; body is
control JSON or an ArtifactRange. The integration plan lists the routes.
Receivers must enforce 32 KiB control JSON and 1 MiB binary bodies, exact length,
bounded read deadlines and admission before reading a body. Raw request URLs,
headers, bodies and unredacted exceptions must not be logged.

Range bodies carry Content-Length, X-Sonder-Offset, X-Sonder-Size,
X-Sonder-Artifact-Sha256 and X-Sonder-Chunk-Sha256. The API implements explicit
offset/length, not arbitrary HTTP Range syntax. The peer client validates actual
length/digest and rejects redirects. It disables ambient proxies for private
transfers; custom proxy policy is not part of this slice.

`ArtifactTransferClient.upload(stream, spec, command_id)` reads at most one chunk
at a time and resumes from the receiver's durable offset. The caller owns and
authorizes the source stream. `download(expected, destination_service,
command_id, context)` writes into another scoped private store, checks expected
identity and every chunk, and seals only after whole-object verification. Neither
method accepts an arbitrary destination path or extracts/executes archives.

`HttpsArtifactTransferPeer.for_test_loopback(...)` explicitly permits only numeric
HTTP loopback origins for test fixtures. Production composition must never select
that factory. The acceptance transfers 66 MiB over an authenticated loopback
server, restarts a client process, reopens both receiver/cache stores and verifies
the resulting bytes. It is not a test of a real remote node.

## Limits of the guarantee

This is local durable transfer metadata plus verified immutable bytes. File
fsync and SQLite synchronous FULL are used; POSIX directories are also fsynced.
Process-exit/restart recovery is tested. Acknowledged two-copy durability,
machine/power-loss survival, cross-node takeover, fact/memory replication,
embedding/index migration, automatic compute input materialization and consensus
are outside this slice. Payload reservation limits do not reserve physical disk
space in advance. No private-node connection or production deployment is required
or performed by these tests.
