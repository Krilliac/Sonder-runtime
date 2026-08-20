# WP2 LOOP-004 — Durable versus live loop events

## Scope

`sonder_runtime.application.loop_event_classification` defines the application
boundary between durable session facts and ephemeral live loop events. It is a
pure classifier and immutable-envelope module; it does not write to a session
repository, dispatch interception callbacks, or change the existing loop,
event, or repository modules.

## Contract

- `LoopEventClass.DURABLE_SESSION_FACT` identifies session history that may be
  persisted and replayed, such as message, model, tool, lifecycle, approval,
  goal, and artifact facts.
- `LoopEventClass.EPHEMERAL_INTERCEPTION` identifies live loop control-plane
  phases (`pre_step`, `model_request`, `pre_execute`, `execute`,
  `post_execute`, `turn_stopping`, `error`, and `retry`). These observations
  are not session facts.
- `LoopEventClass.EPHEMERAL_CAPABILITY` identifies capability observations such
  as checks, selection, availability, grants, denials, and revocation. They
  describe the current live capability surface and are not durable history.
- `classify_event()` fails closed for unknown event types. Adding a new event
  therefore requires an explicit vocabulary decision instead of an accidental
  persistence default.
- `DurableSessionFact` and `EphemeralLiveEvent` are frozen envelopes and make a
  shallow immutable copy of their payload mapping. They enforce the boundary
  represented by their type but do not perform event-schema or repository
  validation.

## Boundary

This slice deliberately does not import `loop_contract`, the durable event
vocabulary, or `SessionRepository`. Future adapters may translate between
these envelopes and those existing contracts in a separately scoped work
package. Live interception and capability data must not be replayed as durable
session state merely because it was observed during a turn.

## Verification

- `tests/test_loop_event_classification.py`
- `python -m pytest tests/test_loop_event_classification.py -q`
- `python scripts/check_architecture.py`
- `python scripts/check_requirement_evidence.py`
- `python -m compileall -q sonder_runtime`

No specification checkboxes are modified by this document.
