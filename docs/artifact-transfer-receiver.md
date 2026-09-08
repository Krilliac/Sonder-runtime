# Configuring the artifact receiver

The receiver is disabled by default. Enabling it adds opaque immutable-byte
transfer routes to the normal HTTP listener. The current deployment supports
**one dedicated credential and one fixed principal/project/peer grant per
receiver**. It does not provide identity federation, multiple receiver bindings,
automatic data replication, model loading, task migration, or HA failover.

Set a fresh, randomly generated `SONDER_ARTIFACT_TRANSFER_KEY` in the existing
protected secrets environment file or service environment. It must contain
32–512 printable non-space ASCII characters and differ from `SONDER_API_KEY`.
Never put it in TOML, URLs, model messages, or artifact metadata. The global API
key and account tokens do not authenticate transfer operations.

Add the following section to the host's typed configuration, replacing the
identities and expiry with the actual grant approved for that peer:

```toml
[artifact_transfer]
enabled = true
principal_id = "local-owner"
project_id = "approved-project"
peer_node_id = "approved-sender-node"
grant_id = "project-transfer-grant"
grant_revision = 1
# Required: set a future Unix timestamp for the approved grant.
expires_at = 0
can_read = true
can_write = true
max_object_bytes = 268435456
quota_bytes = 1073741824
total_bytes = 2147483648
ttl_seconds = 3600
```

The example intentionally fails validation until `expires_at` is replaced. All
identities are fixed host configuration, not request fields. `peer_node_id` is the
authorized sender identity bound to this key, not an inferred socket hostname.
Possession of this dedicated secret is authentication as that single peer grant;
it does not confer account administration or access to other project scopes.
Use distinct grants/keys on each receiver when configuring two directions.

An optional absolute `store_dir` selects private storage. Otherwise the receiver
uses a sibling of `state.home`, named `<state-directory-name>-artifact-private`.
The store creates private anchored directories and rejects symlinks/reparse
substitution, unsafe ownership/ACLs, and overlap in either direction with any
model-writable root. Typed workspace roots and state home are checked too. A
broad allowed root may encompass that default; then choose a separate private
location. Do not exempt the store from root isolation. Startup fails before the
listener opens if the enabled binding, dedicated secret, or private store is
invalid. Disabled receivers create no store.

The existing external-listener TLS requirements still apply. Actual TLS sockets
are accepted. Plain HTTP is accepted only from a loopback socket peer or an
explicitly trusted proxy socket peer when the host enables
`server.tls_terminated_by_proxy`. That flag is the operator's deployment promise
that the configured proxy accepts secure external traffic and cannot be bypassed;
restrict backend network access accordingly. `Forwarded` and
`X-Forwarded-Proto` never establish TLS or principal authority. Direct remote
plaintext requests are refused even if they claim HTTPS in a header.

## Applied configuration and revocation

The bootstrap binding consumes the host's currently applied typed configuration
through an injected provider. Authentication creates a private immutable proof;
no bearer enters the application operation context. Every service authorization
compares the current key, enabled state, fixed grant, expiry, and scope. The HTTP
boundary checks again after body reads, and asynchronous sealing uses the same
live authorizer before publication. Applying a changed binding invalidates old
contexts. Disabling/rotating/revising a grant therefore cannot allow an old
verification context to publish successfully.

Editing TOML or a secrets file alone is **not** a hot reload. Use the host's
configuration application path or restart the service to apply changes. Normal
`serve` startup composes the receiver from typed configuration and shutdown waits
for its verifier to stop. Reapplying configuration through
`configure_typed_config` replaces the binding and revokes previous contexts.
Custom hosts must provide a live configuration source, call `start()` before
exposure, and call `close()` on shutdown. A custom live provider that changes
store or service-limit settings must restart/recompose the service; requests
return `RESTART_REQUIRED` instead of silently using stale settings.

## Wire operations

Every operation requires `Authorization: Bearer <dedicated key>`:

| Method and path | Input |
| --- | --- |
| `POST /v1/artifact-transfers` | JSON `spec` and `command_id` |
| `GET /v1/artifact-transfers/{id}` | No body/query |
| `PUT /v1/artifact-transfers/{id}/chunks/{offset}` | Raw bytes, `Content-Type: application/octet-stream`, `X-Sonder-Chunk-Sha256` |
| `POST /v1/artifact-transfers/{id}/seal` | JSON `command_id` |
| `POST /v1/artifact-transfers/{id}/abort` | JSON `command_id` |
| `GET /v1/artifacts/{id}` | No body/query |
| `GET /v1/artifacts/{id}/bytes?offset=N&length=M` | Bounded range, no body |

`spec` has exactly `sha256`, `size_bytes`, and `media_type`. IDs are the 32-character
lowercase hexadecimal IDs returned by the receiver. Encoded IDs, duplicate query
keys, unknown fields, body-supplied authority, duplicate authorization/framing
headers, and transfer encoding are rejected. Control bodies require JSON and
`Content-Length`, with a 32 KiB ceiling. Chunks require `Content-Length`, with a
1 MiB ceiling; a smaller host `server.max_request_bytes` also applies. Eight
process-wide transfer request slots are acquired nonblockingly before any body
read and held through response delivery. Saturation returns 429 without reading
or draining the body. This bounds admitted transfer bodies/work, not the HTTP
listener's total connection threads or unrelated routes.
Limits are checked before reading; rejected bodies are not drained
and the connection closes. Range output includes content length and
`X-Sonder-Offset`, `X-Sonder-Size`, `X-Sonder-Artifact-Sha256`, and
`X-Sonder-Chunk-Sha256`. Transfer logging omits raw URLs, headers, bodies, keys,
and private proofs.

Disabled/unavailable bindings return 503, bad credentials 401, disallowed grants
403, scope-hidden/missing records 404, and capacity pressure 429. A verifying
seal returns 202; inspect its durable receipt for the actual state. Unfinished or
revoked verification is not success. See [artifact-transfer.md](artifact-transfer.md)
for resumability, command idempotency, integrity and retention limits.
