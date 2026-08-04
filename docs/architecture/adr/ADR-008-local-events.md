# ADR-008: Local domain events without a message broker

**Status:** accepted

## Context

The runtime needs auditable, low-cardinality events (SPEC-2 operations
store, SPEC-3 domain events) but is a single process on a single host.

## Decision

Events go through the EventSink port into operations.db. Events required
for recovery are appended in the same database transaction as the owning
state; observability copies are written afterward and never determine
business success. No broker, no queues.

## Consequences

Event codes are stable identifiers (CHAT_COMPLETED,
AUTOPILOT_STATE_CHANGED, UPDATE_COMMITTED, ...). Payloads carry
identifiers, counts, hashes, durations — redaction is enforced at the
store, and a redaction failure replaces the payload rather than leaking.
