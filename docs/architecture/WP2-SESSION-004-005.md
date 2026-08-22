# WP2 SESSION-004/005 — Replay foundation

This slice adds the pure application replay boundary in
`sonder_runtime/application/session/`. It consumes the existing durable
`DomainEvent` record shape; it does not change the outbox, persistence
adapters, HTTP interfaces, or the observational `EventSink` contract.

`replay_session(events)` validates a single aggregate stream, requiring unique
contiguous sequence numbers, then reconstructs three values: the exact
`model.requested`/`prompt.snapshot` request, the ordered transcript, and a
bounded operational `SessionProjection`. Events are sorted by durable sequence
before projection, so equivalent input order produces equivalent output.

The request is never synthesized from current configuration or process state.
If no durable request snapshot exists, replay returns `request=None`. Request
options are exposed as an immutable mapping, and all result objects are frozen;
there is no module cache, clock, environment read, adapter call, or write-side
side effect. Malformed, gapped, duplicate, or cross-session streams fail closed
with `IntegrityFailure`.

This is the reconstruction foundation only. Persistence integration, session
event vocabulary expansion, checkpointing, repair, export, and HTTP exposure
remain later work.
