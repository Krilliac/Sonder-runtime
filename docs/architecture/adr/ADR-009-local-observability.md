# ADR-009: Bounded process-local observability

**Status:** accepted

## Context

`operations.db` is the durable audit and recovery event store. The
response-scoped `activity_tracker` is the user-visible tool and file evidence
ledger. Neither is a suitable place for cheap process-local counters, latency
windows, and a small recent-event view.

## Decision

The composition root decorates its existing `OperationsEventSink` with
`LocalObservabilitySink`. The adapter keeps bounded in-memory structured events,
low-cardinality counters, bounded latency samples, and explicit eviction,
cardinality, redaction, and local-failure counts. It accepts an injected
clock and has no worker thread.

The authoritative delegate receives the original event first. Free-text
summaries are not retained by the local observer.
Detail keys associated with prompts, content, reasoning, secrets, environments,
process arguments, commands, and output streams are replaced wholesale. Other
fields are recursively bounded under one aggregate node/byte budget and passed
through the production redactor. Only plain built-in containers and scalar
types are inspected; hostile or custom objects are replaced without invoking
their conversion hooks.

## Authoritative boundaries

- `operations.db` remains the durable audit and recovery authority.
- `activity_tracker` remains the response/tool evidence authority.
- Local observability is a disposable process cache. It never controls business
  success, authorization, routing, retry, or recovery.
- A counted delegate-call exception means only that the call raised. The legacy
  durable adapter intentionally swallows storage failures, so local
  observability never claims delivery attestation.
- There is no telemetry exporter, network/cloud path, persistence, background
  daemon, or agent-facing mutation tool.
- Read inspection is by direct host-side Python calls to `snapshot()`,
  `recent_events()`, and the read-only `trace_projection()` adapter. The latter
  consumes only the already-redacted retained event view and does not export it.

The module is under the already packaged `sonder_runtime` tree, so the existing
manifest-only packager includes it without adding a broader package allowlist.
