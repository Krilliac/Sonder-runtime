# WP1 fifth migration slice: evaluation-history adapter

**Status:** Implemented locally; focused verification passed

## Scope

Retire the root `eval_history.py` compatibility alias. The authoritative
implementation is `sonder_runtime.adapters.evaluation_history_store`, and
production entry points already import that adapter directly.

## Completed work

- [x] Remove the root compatibility alias.
- [x] Remove `eval_history` from the compatibility-root allowlist.
- [x] Add the root path to the permanent retired-module architecture ratchet.
- [x] Rewire the dedicated evaluation-history tests to the adapter.
- [x] Extend the isolated architecture regression test to cover this retired root.

## Verification

- `python -m pytest -q tests/test_eval_history.py` — 21 passed.
- `python scripts/check_architecture.py` — passed.
- `python scripts/check_requirement_evidence.py` — passed.
- `git diff --cached --check` — passed.

The master-spec requirements remain unchecked; this slice is migration evidence,
not proof of a complete end-state requirement.
