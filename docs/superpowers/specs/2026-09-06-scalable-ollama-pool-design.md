# Scalable Ollama Pool Design

**Status:** Proposed design. This document changes no production behavior.

## Decision

Replace the source-level 16-worker ceiling in two separate stages.

1. Make the maximum number of workers an explicit, validated per-process
   configuration value. Preserve the current default of 16 total workers and
   support a tested upper bound of 256 total workers.
2. Add dynamic worker membership only after an external, authenticated,
   revisioned membership provider exists. A coordinator still admits a bounded
   active roster and fails closed when membership evidence is stale or invalid.

The first stage supports larger private static deployments with predictable
resource use. The second supports controlled elastic enrollment. Neither stage
implements distributed model execution, automatic ownership takeover, or an
unbounded system.

## Current evidence

| Area | Current behavior | Consequence |
| --- | --- | --- |
| Worker count | The Ollama pool has a maximum of 16 total workers and configuration allows 15 additional worker origins. | A coordinator cannot represent a 17th configured worker. |
| Identity | Worker identity is currently derived from host and port. | It labels static endpoints but is not a durable membership identity. |
| Admission | One condition protects per-worker inflight counts and one pool-wide waiter count. | Queue depth is a shared pool limit, not a queue per worker. |
| Routing | Model capability, least-inflight work, and latency choose one worker. | One complete request goes to one worker; response-bearing work is never replayed. |
| Probing | Stale probes use at most four concurrent calls but schedule the full stale list. | Larger rosters need fair probe batches to avoid fleet-size refresh latency. |
| Metrics | Worker metric labels are bounded to sixteen plus overflow. | Metric cardinality is safe even though status currently enumerates all workers. |
| Status | Pool status and terminal status render every worker. | Large rosters need paged, bounded operator output. |
| Capability reporting | Request-level pooling is explicit; model sharding and indefinite scale are unavailable. | These statements must remain true after this work. |

Typed capacity propagation is a prerequisite. Canonical typed composition must
pass configured inflight and queue limits directly to the pool. Stale
compatibility environment values must not override them.

The compute fabric and scheduler-partition contracts are useful references for
bounded probes, pages, revisions, and synthetic 16, 64, and 256 node tests.
They are not inference membership authority. A compute node may run a job; an
Ollama worker may serve a model request. The two authorities remain separate.

## Goals

- Let a private operator set a larger explicit worker-roster limit without a
  source change.
- Preserve finite worker state, queue, probe concurrency, status pages, and
  metric labels.
- Keep remote-Ollama consent, HTTPS requirements, trusted-origin policy, and
  no-proxy transport behavior unchanged or stricter.
- Give the app, REPL, and HTTP clients a truthful summary of configured,
  eligible, draining, unhealthy, and omitted workers.
- Provide a separately testable path to dynamic enrollment with explicit
  authority, identity, lease, revision, and failure rules.

## Non-goals

- Split model weights, KV cache, or one request across Ollama workers.
- Treat DNS, mDNS, a health probe, or a worker model list as authority to
  enroll a worker.
- Retry a request after any response bytes were returned.
- Reuse inference membership for compute placement, memory replication,
  artifact transfer, quorum, or control-plane takeover.
- Claim automatic failover, automatic failback, or indefinite scale.

## Alternatives

| Approach | Benefits | Risks and limits | Decision |
| --- | --- | --- | --- |
| Static configurable capacity | Smallest change; current private operator workflow and consent model remain intact; simple rollback. | Operators edit and deploy a roster manually; one process still owns a bounded list. | Implement first. |
| Dynamic membership and admission | Controlled enrollment, revocation, and leases; larger fleets without editing every coordinator config. | Requires independent membership authority, durable identity, authenticated transport, reconciliation, and failure handling. | Design now; implement after static acceptance evidence exists. |
| Remove all limits | Superficially simple. | Allows unbounded memory, status, probes, queues, and metric work. | Reject. |

## Recommended architecture

Membership authority and request scheduling have different jobs. A membership
source says which workers a coordinator may consider. The pool decides whether
one bounded request may use one eligible worker.

    Typed static config or external registry
                    |
                    v
           Membership verifier
                    |
                    v
           Immutable roster revision
                    |
                    v
           Membership controller
                    |
                    v
           Ollama worker pool
                    |
                    v
      Bounded admission and whole-request routing

### Stage 1: bounded static roster

Add a field named worker_pool_max_workers to the typed Ollama configuration.
It includes the primary Ollama endpoint and defaults to 16, preserving current
behavior. Validation canonicalizes origins, rejects duplicates, and rejects a
unique roster larger than the configured limit.

The initial allowed interval is 1 through 256 total workers. It means that
one coordinator has a tested finite roster bound; it does not promise that a
machine can service 256 workers or that 256 machines will deliver a given
throughput.

The pool keeps one global queue. It must not multiply queue depth by worker
count, because that would turn one operator budget into fleet-size-dependent
memory and latency.

Static-roster requirements:

- Replace the fixed worker-count check with an instance-level maximum.
- Keep per-worker inflight at 1 through 64.
- Keep queue depth at 1 through 4096 across the whole pool.
- Retain the sixteen-label metric limit and aggregate later workers under the
  overflow label.
- Probe stale workers in fair, bounded batches.
- Return a stable summary and cursor-paged detail. Terminal output shows a
  bounded first page and omitted-record count.

### Stage 2: externally admitted dynamic membership

Dynamic membership adds a narrow application port instead of teaching the pool
to discover nodes. Its input is a membership snapshot containing:

- cluster identity;
- strictly increasing generation;
- issue time and expiry time;
- protocol version and issuer identity;
- a bounded set of worker advertisements.

Each worker advertisement contains an opaque stable worker identity, HTTPS
origin, member generation, lifecycle state, supported model keys, and
advertised capacity. Identity is never derived from origin.

The external adapter owns mutually authenticated TLS, issuer or signature
verification, trust-anchor configuration, response-size limits, redirect
refusal, and clock checks. A pure domain contract owns shape validation,
duplicate identity rejection, monotonic generation, expiry bounds, and
immutable snapshot semantics. The pool sees only a validated roster and never
contacts a registry while holding its admission lock.

The membership controller owns one runtime-managed refresh loop. It obtains a
snapshot outside the pool lock, validates it, then atomically reconciles the
roster. It never accepts endpoints from a model request, generic response
body, DNS answer, or capability probe.

### Worker lifecycle

| State | New admission | Existing request | Transition |
| --- | --- | --- | --- |
| probation | Blocked | None | First attested capability probe succeeds. |
| active | Allowed when model and capacity match | Allowed | Normal state. |
| draining | Blocked | Allowed until original request deadline | Removal, revocation, endpoint replacement, or explicit drain. |
| expired | Blocked | Follows original deadline | Lease or trusted snapshot expires. |
| unhealthy | Blocked while circuit is open | None | Transport or incompatible capability failure. |

An endpoint change for a stable member requires a new member generation and
fresh membership verification. The old endpoint drains; it is never silently
changed below an in-flight request.

### Outage and partition behavior

- A malformed, untrusted, expired, or replayed snapshot is rejected and does
  not modify the last accepted roster.
- A last trusted snapshot is usable only until its own expiry. After expiry,
  every remote member is expired and receives no new work.
- A local primary remains usable during membership-source outage only when
  local_fallback is explicitly true. The default is false.
- A source outage never creates a member and never promotes a compute node,
  replica, or controller.
- Revocation or removal drains a worker immediately. An in-flight request is
  never duplicated because membership later changed.
- No eligible model returns the existing capability-unavailable error. No
  capacity uses the existing queue and backpressure policy. No available
  worker returns the existing pool-unavailable error.

## Configuration contract

The static Ollama fields below preserve the current small private deployment
defaults.

| Field | Default | Range | Meaning |
| --- | --- | --- | --- |
| worker_pool_max_workers | 16 | 1 through 256 | Maximum unique primary-plus-worker roster held by one coordinator. |
| worker_max_inflight | 1 | 1 through 64 | Maximum concurrent requests per eligible worker. |
| worker_queue_depth | 32 | 1 through 4096 | Maximum waiting requests across the pool. |
| worker_capability_probe_parallelism | 4 | 1 through 8 | Maximum in-process capability probes for this pool. |
| worker_capability_probe_batch_size | 32 | 1 through 128 | Maximum stale workers selected in one refresh pass. |
| worker_status_page_size | 32 | 1 through 128 | Default administrative worker-detail page. |

The equivalent environment names are SONDER_OLLAMA_POOL_MAX_WORKERS,
SONDER_OLLAMA_WORKER_PROBE_PARALLELISM,
SONDER_OLLAMA_WORKER_PROBE_BATCH_SIZE, and
SONDER_OLLAMA_WORKER_STATUS_PAGE_SIZE. Typed composition takes precedence
over ambient environment state. An explicitly injected environment map remains
a compatibility test seam.

Dynamic mode is off by default. Its configuration requires a mode, nonempty
cluster identity, protocol version, refresh interval, bounded snapshot size,
and an explicit local-fallback decision. External mode additionally requires a
membership adapter, trust anchor, and mutually authenticated transport. A
registry URL alone cannot activate membership.

The dynamic snapshot maximum bounds a registry response. The active roster
remains bounded by worker_pool_max_workers. When a snapshot contains more
eligible members than the active limit, the controller uses deterministic
documented selection and reports both counts. It must not silently discard
members.

## Routing and status

Routing remains latency-aware least-inflight inside the active roster. Only
workers whose membership, capability, circuit, and capacity state are eligible
are candidates.

Pool status becomes versioned and splits totals from one bounded detail page.
It includes roster generation, membership mode and state, configured,
eligible, draining, unhealthy, and available-capacity counts; the global queue
state; a bounded worker list; an opaque next cursor; and a complete flag.

Worker details contain stable identity, lifecycle state, inflight capacity,
bounded error class, and capability freshness. They never contain credentials,
TLS material, prompts, response bodies, workspace paths, or unbounded model
lists. Configured origins remain administrator-only.

The REPL and app show eligible/total workers, queue occupancy, available
capacity, and one of static, membership-current, membership-stale, or
membership-expired. They keep the statement that each request is whole-worker
placement. Operational capabilities keep model sharding and indefinite scale
unavailable.

## Test and acceptance strategy

### Static capacity

- Defaults retain 16 total workers and reject a seventeenth unique member.
- Typed 64-worker and 256-worker configurations ignore stale ambient capacity
  environment values.
- A 64-worker configured limit rejects a sixty-fifth unique origin.
- Duplicate normalized origins are rejected before scheduling.
- Queue depth remains global as roster size changes.
- Probe launches do not exceed configured parallelism or batch size; fake
  clock tests prove fair progress through multiple batches.
- Status ordering, cursor validation, roster-generation invalidation, output
  size, and secret/body redaction are tested.
- Metrics retain sixteen worker labels plus overflow.

### Dynamic membership

- Domain tests reject duplicate identities, bad protocol versions, expiry
  inversions, large snapshots, and generation rollback.
- Adapter tests reject redirects, bad client identity, untrusted issuer,
  oversized body, signature failure, certificate binding mismatch, and
  non-HTTPS member origin.
- Controller tests use an injected clock and source for active, draining,
  expired, revoked, endpoint-replaced, and outage behavior.
- Pool tests prove that a roster update never changes an in-flight endpoint or
  retries response-bearing work.
- Synthetic 16, 64, and 256 member runs check bounded probe concurrency,
  pagination, capacity accounting, queue behavior, and deterministic eligible
  selection.

### Live evidence

Before external mode is enabled on a private cluster, run a disposable
two-host test with TLS identities, a known revocation, expired membership,
worker saturation, and a response-bearing interruption. Record exact config
and code revisions, identities, results, and cleanup. The record proves only
that tested profile.

## Migration

- Existing worker-list deployments behave unchanged because the default
  maximum is 16.
- Environment-only composition remains supported; typed canonical roots pass
  parsed values directly.
- Existing count readers keep working. A schema-version reader handles new
  paged detail fields.
- The static stage needs no database or model migration.
- External mode is opt-in. Rollback is static mode plus deployment of the
  prior static roster.
- Roll out at 16, then a 64-worker static canary, then a 256-worker synthetic
  acceptance result. External mode starts only after separate provider and
  live-cluster evidence passes.

## What this does not prove

An explicit 256-member coordinator is not infinite scale. Dynamic membership
is not infinite scale either. Both retain process memory, queue, probe,
timeout, identity, registry, and network limits.

This work does not prove multi-host model weights, tensor or pipeline
parallelism, shared KV cache, automatic job/memory/artifact migration,
replication, quorum, old-owner fencing, automatic takeover, automatic
failback, or untested-host throughput and recovery.

The accurate statement after the proposed work is: Sonder supports bounded,
private, whole-request placement over a configurable static roster, with an
optional future externally admitted roster. It does not claim indefinite or
model-sharded execution.

## Review checklist

- Every remote endpoint still passes consent and TLS checks.
- Membership validation is separate from capability probes and scheduling.
- Network work happens outside pool locks.
- Every inventory and status surface is bounded and paged.
- Queue, probe, metric, and log cardinality remain bounded at 256 and under
  external-membership test inputs.
- App and REPL retain explicit unavailable sharding and indefinite-scale
  states.
- Static configuration is the default; dynamic mode requires authenticated
  authority.
