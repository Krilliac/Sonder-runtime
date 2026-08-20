# WP2 SESSION-001 — Typed identities

## Boundary

Added immutable, validated value objects for `SessionId`, `TurnId`, `StepId`,
`CallId`, `AgentId`, `JobId`, `ArtifactId`, and `OperationId` in
`sonder_runtime/domain/common/ids.py`.

Each type uses the existing `prefix_<32 lowercase hexadecimal characters>`
representation. `new_id(prefix)` remains a plain-string generator, while
each typed ID supports `new()`, `from_serialized()`, `serialize()`, `str()`,
and the existing `is_id()` compatibility predicate. Construction rejects a
wrong prefix, uppercase/non-hex payload, incorrect length, and non-string
values.

## Scope

This slice changes only the shared ID module, its focused tests, and this
evidence document. Persistence, replay, and interface layers are intentionally
out of scope.

## Evidence

- `tests/test_domain_ids.py` covers all eight types, generation, validation,
  stable serialization round trips, and legacy helper compatibility.
- Focused test command: `python -m pytest tests/test_domain_ids.py`.
- No specification checkboxes are modified by this document.
