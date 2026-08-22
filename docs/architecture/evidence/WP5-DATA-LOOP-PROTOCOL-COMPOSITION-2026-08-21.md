# Data, loop/session, and protocol composition — 2026-08-21

This batch adds three provider-neutral application boundaries:

- `PersistenceFacade` and SQLite CAS/graph adapters enforce domain ownership,
  no cross-database transaction claims, atomic record/outbox writes, revision
  CAS, and deterministic artifact manifests.
- `LoopSessionLifecycleFacade` composes turn/step state, interception versus
  durable facts, steering, cancellation cleanup, retries/idempotency, and the
  remaining session fork/repair/checkpoint/retention operations.
- `ProtocolApplicationFacade` composes event vocabulary, bounded resumable
  streams, reconnect authorization, client schema/mobile parity, MCP, OpenAI,
  editor, and control-plane compatibility contracts.

Focused combined verification passed 100 tests. The boundaries do not claim
external databases, live LSP/provider/client calls, or platform deployment
receipts; those remain explicit integration obligations.
