# Control-state rehearsal design

## Goal

Add an explicit, disposable command that can collect external control-state
replication and fencing evidence for a configured two-node private pool. The
command must never start automatically, promote an owner, change a lease, or
make the installed runtime claim high availability.

## Scope

The existing `HttpsControlStateProvider` and
`ExternalControlStateCoordinator` already validate bounded authenticated
requests, exact acknowledgement bindings, and fencing receipts. They are
library-only seams. This slice adds an opt-in configuration and command
composition path so an operator can exercise those seams against a separately
run provider in a disposable rehearsal namespace.

The command constructs a provider only after configuration validation and an
explicit command invocation. It writes a redacted report containing identities,
event sequence, acknowledgement shape, fencing decision, and an explicit
`promotion_attempted: false` marker. It does not construct a runtime owner,
call a promotion API, or alter local SQLite ownership.

## Configuration and secret boundary

`[control_state_rehearsal]` is disabled by default. It identifies the cluster,
the local node, a distinct witness identity, the provider identity and origin,
and bounded request limits. The two data replicas are derived from the existing
validated pooled-pair deployment configuration: `compute.node_id` plus its one
configured peer. The provider origin is HTTPS except for an explicitly enabled
loopback-only test endpoint.

The API key is only read from `SONDER_CONTROL_STATE_REHEARSAL_API_KEY` in the
normal secrets-environment boundary. TOML, status, diagnostics, and the report
never expose it. Missing credentials, a non-two-node profile, duplicate data
and witness identities, a non-loopback HTTP origin, or an ordinary runtime
startup all fail closed.

## Command behavior

`python -m sonder_runtime control-state-rehearsal` requires the explicit
configuration and an operator-supplied bounded resource scope. It submits one
event, reads the authoritative page, and, only with a confirmation flag,
requests a fencing receipt and evaluates readiness. A positive readiness result
means only that the external provider returned matching evidence. The command
always reports that promotion is outside Sonder and was not attempted.

The command exits nonzero for unavailable, malformed, mismatched, unauthenticated,
or ambiguous provider evidence. It does not retry ambiguous writes, fall back
to local SQLite, or infer a witness from either data node.

## Rehearsal evidence

The end-to-end test runs a loopback provider and two fresh child processes with
separate homes, identities, and evidence files. The provider is a test fixture,
not a deployed witness. The success case proves only that the client crosses an
out-of-process network boundary and preserves identity/receipt checks. Failure
cases prove an unavailable or invalid provider stops the exercise and causes no
promotion action. The test uses an explicitly permitted insecure loopback
origin; non-loopback configuration remains HTTPS-only.

This does not establish a real independent failure domain, consensus election,
automatic failover or failback, authoritative memory/artifact migration, or
model-weight sharding. A production witness still needs its own deployment,
durable replicas, fencing authority, and live failure evidence.

## Verification

Tests first cover disabled/default configuration, secret handling, origin and
identity validation, normal-runtime non-composition, successful command evidence,
and each fail-closed provider failure. The subprocess fixture asserts separate
PIDs and state paths, bounded completion, cleanup, and zero promotion calls.
Focused command/config/provider tests, the existing topology and coordinator
suites, documentation checks, and a hosted exact-head matrix are required before
merge.
