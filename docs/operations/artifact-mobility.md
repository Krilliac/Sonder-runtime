# Explicit fixed-peer artifact copy

Artifact mobility copies one pre-admitted, sealed source artifact to one
configured receiver. It is operator-invoked content transport. It does not move
jobs, models, memory ownership, sessions, node roles, or scheduler leases, and
does not provide automatic migration, retries, discovery, failover, or takeover.

## Provisioning and authority

The host owns separate settings: `artifact_mobility_source` admits local source
bytes, `artifact_mobility` fixes the outbound destination, and the remote host's
`artifact_transfer` grants receiver access. Enabling a source spool does not
enable a receiver or add HTTP routes. Private source and journal storage must
stay outside workspace/file-tool authority and separate from receiver storage.

Only an approved in-process workload receives the host's exact source publisher
capability. Its sealed source ID is the operator input. There is no source-file
staging CLI, public publisher factory, MCP tool, or HTTP/REPL mutation command.
Host capability references are cooperative composition, not isolation against
untrusted code running in that same interpreter.

The CLI uses the existing host-owned Application. On first start it loads only
the OS-account canonical `sonder.toml` and `sonder.env`, with an explicit absolute
`state.home`. The canonical directory is OS LocalAppData/sonder on Windows,
the account's Library/Application Support/sonder on macOS, and the account's
.local/share/sonder elsewhere. Missing or invalid provisioning fails closed.
Ambient config/home/key environment overrides and per-invocation selectors
cannot choose mobility authority. A running graph remains pinned until an
explicit host restart/reset. See [configuration](configuration.md).

## Local commands

```text
python -m sonder_runtime artifact-mobility send --source-artifact ID --confirm-destination node-b --json
python -m sonder_runtime artifact-mobility resume --operation-id ID --json
python -m sonder_runtime artifact-mobility status --operation-id ID --json
python -m sonder_runtime artifact-mobility list --json
```

Send generates its operation ID. Source and operation IDs are exactly 32
lowercase hexadecimal characters. Confirmation uses the configured public label:
1–128 ASCII characters, alphanumeric first, then alphanumerics, `_`, or `-`.
`node-b` is valid; URLs, paths, dots, colons, whitespace, and Unicode are invalid.
Parsing rejects malformed values and forbidden selectors before help or host
access using a fixed redacted error. The post-host confirmation comparison is
exact. The label never authenticates or selects a peer.

Destination authority includes the fixed HTTPS origin, TLS leaf pin, recipient
attestation, source-owner grant, dedicated credential, and immutable binding.
Certificate checking precedes bearer transmission; attestation and immutable
transfer-bound receipt checking precede append. A static object-size cap does
not prove available quota or capacity at begin.

## Interruption and recovery

Each invocation holds a nonblocking OS operation lock and a current durable
lease. Lost responses do not start background retries. Status, list, capability
inspection, construction, and close do not contact a peer. Close does not cancel
receiver work or clear an unexpired lease.

Explicit resume recovers expired leases locally, then attempts only the same
eligible operation. A live lease or retained OS lock prevents replacement
dispatch. Resume uses the same canonical remote command and validates the
receiver's durable offset before continuing. It may replay an idempotent begin
when no local receipt checkpoint survived; it must not resend accepted bytes.
It rechecks source, credential, certificate, and recipient fences and cannot
redirect an old operation after rotation.

| State | Operator action |
| --- | --- |
| `ready` | Explicitly resume when ready to dispatch. |
| `dispatching` | An invocation/lease owns the attempt. Wait for completion or expiry; do not edit the journal. |
| `resumable` | Resolve the interruption and explicitly resume. |
| `awaiting_seal` | Receiver verification is pending. A later explicit resume may confirm sealing. |
| `retryable_blocked` | Resolve availability/capacity and explicitly resume. |
| `sealed` | Receiver confirmed the exact immutable specification. Source retention remains independent. |
| `terminal_blocked` / `expired` | Do not resume or reuse the ID. Resolve provisioning and start a new operation if appropriate. |

Terminal IDs retain tombstones after detailed receipts are pruned. Do not delete
the source or journal on the assumption that copying transfers ownership.

## Public evidence and limits

CLI, Application, and read-only REPL projections contain only operation ID,
source artifact ID, destination label, state, created/updated timestamps, and a
constrained outcome code. Origin, paths, pins, binding HMAC, credential generation,
bearer, payload, and peer-generated messages are excluded. Invalid persisted
labels fail integrity validation before projection.

The composed test uses two local receiver processes and an explicitly injected
numeric-loopback factory with a synthetic certificate. It rehearses durable
offsets, source/journal reopening, expired-lease recovery, manual resume, exact
sealing, and rejection of a distinct recipient. It does **not** prove real TLS
pinning, independent-host availability, replication, or failover. Deployment
still requires the separate [two-host acceptance plan](artifact-mobility-two-host-acceptance.md).
