# WP1 queued-actions root redirect evidence

## Closure item

`queued_actions.py` is now an identity-preserving redirect to
`sonder_runtime.adapters.persistence.queued_actions`. The root name remains
only because the immutable `migrations/queued_actions/0001_baseline.py`
imports it to read `_SCHEMA`; production callers already use the packaged
adapter. This removes the duplicate root delegate namespace without changing
the historical migration artifact or its database behavior.

## Verification

- `tests/test_queued_actions_compatibility.py` verifies root and packaged
  imports resolve to the same module object and that the migration baseline
  remains importable through the compatibility name.
- The focused test also verifies the canonical schema remains exposed at the
  historical import boundary.
- No production import of the root module was found outside the immutable
  migration baseline; `scripts/check_architecture.py` continues to model that
  baseline as the sole allowed root import.

## Write set

Only `queued_actions.py`, its focused compatibility test, and this evidence
document are part of this closure item.
