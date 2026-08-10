# ADR-004: Transactional Outbox Pattern

**Status:** Accepted  
**Date:** 2026-08-09  
**Context:** SPEC-5 §9, M10

## Decision

Every state-owning database (memory.db, automation.db, training.db, selfmod.db, updates.db) includes an `outbox_events` table. State mutations and their corresponding events are committed atomically in a single SQLite transaction. A LocalEventDispatcher polls unpublished events and projects them into operations.db. Delivery is at-least-once with aggregate-local ordering and idempotent consumers.

## Rationale

Without a transactional outbox, domain events can be lost on crash between state commit and event publication. The outbox pattern guarantees that committed state always has a corresponding durable event, without requiring an external broker (W7).

## Consequences

- Each state-owning DB gains an outbox_events table
- operations.db stores imported events with UNIQUE(source_event_id) for dedup
- No cross-database transactions (M9)
- No external broker (Kafka, RabbitMQ, Redis)
