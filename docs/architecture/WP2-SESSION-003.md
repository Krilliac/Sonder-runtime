# WP2 SESSION-003: Durable session event vocabulary

## Scope

`sonder_runtime.domain.common.events` defines the domain contract for durable
session events. It adds the append-only `EventKind` vocabulary and the
validated `DurableEvent` envelope without changing repositories, databases,
outboxes, dispatch, or storage formats.

The vocabulary covers lifecycle, messages, context, prompts, model and tool
activity, approvals, goals and plans, compaction, retrieval, subagents,
cancellation, errors, and artifacts. Each kind has a required/optional
payload schema. Unknown kinds, missing or extra fields, wrong types, non-finite
numbers, non-JSON values, invalid timestamps, and unsupported schema versions
are rejected with `EventValidationError`.

## Durable shape

The serialized record contains `schema_version`, `event_id`, `kind`, aggregate
identity, monotonic aggregate `sequence`, optional session/correlation IDs,
UTC `occurred_at`, and a validated `payload`. `to_json()` uses sorted keys and
compact separators for deterministic evidence and transport.

Payloads intentionally use bounded references such as `content_ref`,
`summary_ref`, and `results_ref`; raw prompts, model output, secrets, and tool
arguments are not part of this vocabulary.

## Compatibility boundary

The pre-existing untyped `DomainEvent` remains available for current SPEC-5
outbox callers. New session code should use `DurableEvent`. Storage adapters
may later translate `DurableEvent.event_type` and `payload` into their existing
records; that migration is outside SESSION-003.

## Evidence

- `tests/test_session_event_schema.py`
- `python -m pytest tests/test_session_event_schema.py -q`
- `python scripts/check_architecture.py`
- `python scripts/check_requirement_evidence.py`
- `python -m compileall -q sonder_runtime`
