# Bounded, explicit fact replication

This runbook describes the currently implemented trusted-peer memory boundary.
It is a deliberately small data-copy feature for one operator or trusted team
running a private cluster. It is not a general memory-mobility, cluster
ownership, or high-availability feature.

## Implemented scope

Only an **explicitly journaled, project-scoped `fact` mutation** can enter the
source stream. The supported source is `SQLiteAuthoritativeFactSource`, used
through an injected unit of work; it writes the materialized fact, source
state, and journal mutation in one SQLite transaction. A normal legacy fact
write that did not use that source is not retrospectively discovered or
copied.

The receiving host applies a validated page to its local replication journal
and normal fact projection before it creates its durable receipt. A receipt
therefore means that the supported target fact row has been projected locally
for that page. It does not say anything about interactions, outcomes,
preferences, lessons, sessions, agent transcripts, embeddings/indexes,
artifacts, configuration, or control state. Those are excluded.

The service has one exact project scope and at most 16 fixed peer identities.
It constructs no outgoing client until the owner explicitly calls
`replicate_once()`. One call exports at most the configured
`max_batch_records`, sends that one page to every configured peer, and leaves
the cursor unchanged unless **every fixed configured peer** supplies a matching
durable receipt. A result of `replicated` is all-fixed-peer receipt evidence,
not a consensus or quorum decision. A stopped or invalid peer leaves the page
`pending` with a stable failure reason; a later send occurs only after another
explicit `replicate_once()` call.

There is no background retry, polling, discovery, enrollment, caller-selected
peer/scope/cursor, ownership migration, model or data sharding, automatic
memory migration, automatic takeover, automatic failback, promotion,
fencing, quorum, or unbounded scaling. A successful data copy never grants
authority over another host.

## Single-PC and two-PC posture

| Deployment | Supported configuration | What the runtime proves | What it does not prove |
| --- | --- | --- | --- |
| One PC | Leave `[memory_replication].enabled = false`. An enabled service requires at least one fixed outbound peer. | The local SQLite store and any local authoritative fact transaction are durable according to their local storage contract. | A second durable copy, peer receipt, recovery after host/storage loss, ownership transfer, takeover, failback, or HA. |
| Two PCs in one trusted private cluster | Each host has an enabled, reciprocal fixed-peer configuration for the same exact project scope and a private HTTPS receiver. | A page marked `replicated` has a matching durable receipt from the one configured peer, and the receiver has projected the supported fact page before that receipt. | An independent witness, quorum, fencing, merged control state, automatic 1-to-2-to-1 operation, controller recovery, takeover, failback, or HA. |

The deployment profile (`single-host`/`single-pc` or
`pooled-pair`/`two-pc`) remains a separate control-state and compute
declaration. A private compute pool and a fact receipt do not merge databases
or make either host the replacement authority for the other. See
[deployment topology](deployment-topology.md) for those profile limits.

## Typed configuration and secret boundary

The feature is disabled by default. Its topology belongs only in
`sonder.toml`; environment variables, command-line overrides, discovery, and
caller input cannot add or replace a peer, scope, identity, or origin. Secrets
are accepted only from the configured secrets environment file or process
environment and are rejected from TOML.

This is a directional example for node A. Node B needs its own reciprocal
section: its `local_node_id` is node B, its fixed peer is node A, and its
`accepted_source_ids` contains node A. Replace the placeholder origin with a
real canonical HTTPS origin with an explicit port; it is intentionally not an
endpoint to copy from this document.

```toml
[memory_replication]
enabled = true
local_node_id = "node-a"
project_scope = "project-a"
receiver_enabled = true
accepted_source_ids = ["node-b"]
request_timeout_seconds = 5
max_request_bytes = 8388608
max_response_bytes = 65536
max_batch_records = 256

[[memory_replication.peers]]
node_id = "node-b"
project_scope = "project-a"
origin = "https://<NODE-B-DNS-NAME>:8443"
```

`local_node_id`, every peer `node_id`, and every accepted source are exact
bounded identities. They cannot be empty, duplicated, wildcarded, or equal to
the local identity. The accepted-source tuple must be a subset of the fixed
outbound peers; each peer project scope must exactly equal the local project
scope. The sender permits only canonical HTTPS origins with an explicit port,
no user information, path, query, fragment, wildcard, or ambient proxy. It
does not follow redirects and explicitly disables process proxy settings for
peer delivery.

An enabled receiver must use the host's loopback listener or a declared TLS
proxy. In the reference deployment the Sonder process remains on loopback and
the fixed peer reaches the TLS proxy. Install a certificate chain trusted by
the sending host and whose name matches the configured origin; do not bypass
certificate validation. Plain HTTP is retained only by a lower-level loopback
test adapter and is not a valid typed trusted-peer deployment origin.

Place these values only in the secrets environment source, never in TOML,
shell history, a status record, or a transcript:

```text
SONDER_MEMORY_REPLICATION_KEY=<32-to-512-printable-ASCII-peer-bearer>
SONDER_MEMORY_REPLICATION_STATE_INTEGRITY_KEY=<different-32-to-512-printable-ASCII-local-key>
```

The peer bearer is the credential sent to fixed receivers. It must differ from
the API key, artifact-transfer key, and auth secret. The state-integrity key
is local to each host, is never sent to a peer, and must differ from the peer
bearer and all of those other secrets. `python -m sonder_runtime config --json`
prints a redacted projection: origins are shown only as configured/unset and
secret values only as set/unset.

There is no peer-key overlap or automatic key rollover. A one-sided key change
causes receiver authentication failure, a pending result, and an unchanged
cursor. Rotate deliberately: stop the affected acceptance/deployment owners,
install the same new distinct peer bearer at every current receiver and sender,
restart them, validate the redacted typed configuration, then make one
explicit send. Do not leave the old peer bearer configured as an overlap key.

## Operator, API, REPL, and app visibility

The one outgoing operation is the application-owned
`MemoryReplicationService.replicate_once()` method. It deliberately has no
peer, URL, credential, project, cursor, or retry arguments. This release has
**no public HTTP, MCP, CLI, or REPL command** that invokes it. In particular,
the REPL `/runtime` status command does not contact a peer and must not be
presented as a replication send command. Do not start a second ad-hoc service
process against a live state directory merely to invoke the method.

`POST /v1/memory/replication/batches` is an incoming fixed-peer receiver
endpoint, not an operator send endpoint. It is unavailable when the typed
service is disabled, rejects browser origins, and accepts only the configured
bearer, fixed source identity, exact project scope, and bounded canonical
batch. A successful `202` response contains a durable fact-projection receipt;
it is not a takeover, failback, or HA signal.

For an authorized operator, `GET /v1/sonder/status` exposes the local
`memory_replication` state plus
`operational_capabilities.mobility.memory_replication_transport`. The status
contains the bounded local cursor, receipt peer identities/cursors, and
pending or failed fixed-peer identities/reason codes. It does not expose a
fact payload, origin, state-file path, HMAC, or secret. Its transport reason
states that fixed-peer fact replication is explicit and bounded; its mobility
projection always reports both
`automatic_takeover_available: false` and
`automatic_failback_available: false`.

The Flutter System screen renders the same read-only capability projection.
It shows distinct `Automatic takeover` and `Automatic failback` rows as
unavailable. The screen and API do not start a transfer or infer peer health.

## Local checkpoint and rollback boundary

The enabled service keeps two compact private files under the runtime state
home: `memory-replication-state.json` and
`memory-replication-anchor.json`. They hold the local send cursor, bounded
last receipt/failure evidence, and an authenticated high-water link to the
last acknowledged journal record. The documents are strict canonical JSON,
reject duplicate keys and non-finite values, and use domain-separated HMACs
with `SONDER_MEMORY_REPLICATION_STATE_INTEGRITY_KEY`.

The checkpoint is written first and the anchor second under a bounded local
lock. A result becomes `replicated` only after both write-through replacements
finish; the in-memory cursor moves afterward. Windows requests
`MoveFileExW` write-through replacement after file flush. POSIX flushes the
file and parent directory. A crash or write failure between the two writes
leaves a detectable mismatch instead of silently skipping an unacknowledged
page.

At the next local start, corrupt, partial, malformed, or one-file
checkpoint/anchor rollback becomes a stable local state fault before the
service opens the journal or constructs a peer client. Before each later
explicit send, the high-water anchor is compared with the local source journal
record. A missing or changed record blocks the operation rather than
replaying from a cursor that is no longer proven.

This is not an external rollback oracle. An actor who can replace the entire
runtime state directory with an older, mutually consistent signed
checkpoint-and-anchor snapshot can make that old pair appear valid. If that
threat must be addressed, use an independent external, TPM-backed, or remote
monotonic anchor. Sonder does not configure or claim one.

## Two-host acceptance procedure — unexecuted template

This procedure is a reproducible operator checklist, **not evidence that an
independent two-host deployment has been run by this repository**. The
automated coverage uses bounded in-process and loopback seams. It does not
prove independently hosted TLS reachability, certificate deployment, firewall
policy, or cross-host availability.

Run this only in an isolated acceptance deployment with a disposable
project-scoped fact dataset. Keep a redacted record for each host. It must
include the exact revision and a fingerprint of the redacted configuration,
but never an origin, certificate private key, bearer, state-integrity key,
fact text, or request payload.

| Record field | Node A placeholder | Node B placeholder |
| --- | --- | --- |
| Exact revision | `<NODE-A-GIT-REVISION>` | `<NODE-B-GIT-REVISION>` |
| Redacted configuration SHA-256 | `<NODE-A-REDACTED-CONFIG-SHA256>` | `<NODE-B-REDACTED-CONFIG-SHA256>` |
| Local identity and project scope | `<NODE-A-ID>, <PROJECT-SCOPE>` | `<NODE-B-ID>, <PROJECT-SCOPE>` |
| Fixed peer IDs / receiver enabled | `<NODE-B-ID>, true` | `<NODE-A-ID>, true` |

**Harness precondition.** No public send command exists. The acceptance
harness must receive the application object from the normal typed bootstrap,
run while there is no second owner of that source state directory, and make
only this local call sequence:

```python
service = application.memory_replication
if service is None:
    raise RuntimeError("typed fact replication is not enabled")
service.start()
before = service.status()
result = service.replicate_once()
after = service.status()
```

The harness must record only whitelisted status fields described below and
must not supply a peer, scope, cursor, URL, credential, or retry policy to the
service. It is an acceptance-only in-process seam, not a newly supported
REPL, CLI, MCP, or HTTP interface.

1. On each host, record the revision and validate its typed configuration
   before binding:

   ```powershell
   git rev-parse HEAD
   python -m sonder_runtime config --config <TOML-PATH> --secrets <SECRETS-PATH> --json `
     | Set-Content -Encoding utf8 .\memory-replication-redacted-config.json
   (Get-FileHash -Algorithm SHA256 .\memory-replication-redacted-config.json).Hash
   ```

   Check the redacted file locally before retaining its hash. Record only the
   revision, hash, local identity, exact project-scope identifier, peer-ID
   count, receiver-enabled flag, and key-presence flags. Both hosts must have
   a distinct local identity, the same intended project scope, reciprocal
   fixed peer IDs, and both required keys marked set.

2. Start the normal typed host on both machines. From each sender host, check
   the fixed peer's TLS receiver without a bearer and without a proxy. The
   `OPTIONS` request is only a TLS/route check; it does not replicate a fact.
   In a Windows acceptance shell:

   ```powershell
   curl.exe --noproxy '*' --proto '=https' --tlsv1.2 --silent --show-error `
     --connect-timeout 5 --max-time 10 --output NUL --write-out '%{http_code}\n' `
     -X OPTIONS "$env:SONDER_PEER_ORIGIN/v1/memory/replication/batches"
   ```

   Use a short-lived local environment variable for the origin and do not
   record it. Expect `204` only when the receiver is enabled and the hostname,
   certificate chain, TLS policy, and direct no-proxy route are valid. Do not
   use `--insecure`, accept a redirect, or treat a TCP connection as a TLS
   proof. Repeat in the opposite direction if both hosts receive.

3. Prove fixed admission fails closed using disposable copies of the TOML.
   Change one accepted source to an unconfigured identity, then separately
   change one peer scope so it differs from the local scope. Each
   `python -m sonder_runtime config ... --json` invocation must fail with exit
   code `2` before either host binds. Restore the validated configuration; do
   not turn a failed configuration into an exception list.

4. Prove peer-key rotation rejection in the acceptance environment. First
   write one fresh disposable `fact` through `SQLiteAuthoritativeFactSource`
   in the exact configured project scope; do not use an ordinary legacy fact
   write. Change only node A's peer bearer, restart its local owner, and use the trusted
   deployment-owned operator harness to call the already-owned service's
   `replicate_once()` exactly once. The result must be `pending`, node A's
   cursor must not advance, and node B must have no new projected fact. The
   public status must show only the configured peer identity and a stable
   failure reason, never a key. Restore a mutually configured new peer bearer
   on both hosts, keeping each host's local state-integrity key unchanged and
   distinct, then repeat the configuration validation before the next
   explicit attempt.

5. Exercise a successful page through the supported source contract. After the
   coordinated key restoration in the prior step, call `replicate_once()` once
   on the node A application-owned service for the retained page. If the
   key-rejection step was deliberately omitted, first write one fresh
   disposable `fact` through `SQLiteAuthoritativeFactSource` in the exact
   configured project scope; do not use an ordinary legacy fact write. Retain
   only the result status, source cursor, configured peer ID, receipt peer
   ID/cursor, and a hash of the test fact ID. Expect `replicated` only when
   every configured peer has a matching durable receipt. Then use node B's
   ordinary local fact read to prove the matching project-scoped fact is
   visible after the receipt. Do not inspect the sender's connection as
   evidence of target projection.

6. Prove stopped-peer behavior. Stop node B's receiver or its TLS front end,
   create one more supported authoritative source fact on node A, and make
   exactly one explicit call. Expect `pending`, an unchanged node A cursor,
   the configured node B ID in the failure set, and no spontaneous retry while
   waiting longer than the configured request timeout. Restart node B, then
   make a second explicit node A call. Only that call may retry the retained
   page.

7. Prove local restart restoration. After one successful page, save the
   redacted status values for cursor, receipt peer IDs/cursors, and persistence
   generation. Restart node A normally. Before any explicit send, its local
   status must report restored persistence and the same cursor/receipt
   evidence. Add one new supported source fact and make one explicit call; it
   must use the restored cursor rather than replaying the acknowledged page.

8. Rehearse manual fault handling only on a disposable state home. Corrupt or
   roll back one of the checkpoint/anchor files, restart the source host, and
   confirm the local state is faulted before a peer or journal operation. Stop
   there: do not delete a single file, reset the cursor, or claim automatic
   repair. Preserve the evidence and restore a verified matching state pair
   only under an operator recovery procedure. A whole-directory rollback
   requires the external/TPM/remote monotonic anchor described above; this
   runtime cannot validate it by itself.

9. Record the passed or failed result for every step, plus the exact revisions,
   redacted configuration hashes, local IDs, scope, receipt IDs/cursors, and
   reason codes. State explicitly that the exercise proves only the fixed
   fact-copy path. It does not prove high availability, quorum, takeover,
   failback, ownership migration, sharding, unbounded scale, or recovery of
   any excluded memory/control-state data.
