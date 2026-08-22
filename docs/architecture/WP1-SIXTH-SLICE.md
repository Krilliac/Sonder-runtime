# WP1 sixth migration slice: semantic-recall adapter

**Status:** Implemented locally; focused verification passed

## Scope

Retire the root `recall.py` compatibility alias. Semantic recall is already
implemented by `sonder_runtime.adapters.recall`; application and interface
surfaces use the typed recall service and gateway.

## Completed work

- [x] Remove the root compatibility alias.
- [x] Remove `recall` from the compatibility-root allowlist.
- [x] Add the root path to the permanent retired-module architecture ratchet.
- [x] Rewire dedicated recall tests to the adapter.
- [x] Remove the root module from packaging and nightly self-modification inventories.
- [x] Replace alias-specific live-reload assertions with adapter assertions.
- [x] Extend the isolated architecture regression test to cover this retired root.

The master-spec requirements remain unchecked; this slice is migration evidence,
not proof of a complete end-state requirement.

## Verification

- `python -m pytest -q tests/test_recall.py tests/test_recall_service.py tests/test_package_local_system.py` — 73 passed, 2 skipped.
- `python scripts/check_architecture.py` — passed.
- `python scripts/check_requirement_evidence.py` — passed.
- `git diff --cached --check` — passed.
