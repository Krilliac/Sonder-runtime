# WP1 fourth migration slice: process-liveness adapter

**Status:** Implemented locally; focused verification passed

## Scope

Retire the root `process_liveness.py` compatibility alias. The authoritative
implementation is `sonder_runtime.adapters.process_liveness`; production and
test callers use that adapter directly.

## Completed work

- [x] Remove the root compatibility alias.
- [x] Add the root path to the permanent retired-module architecture ratchet.
- [x] Rewire the Ollama lifecycle test to the adapter import.
- [x] Remove the compatibility-alias assertion from the process-liveness test.
- [x] Extend the isolated architecture regression test to cover this retired root.
- [x] Record the execution-status import repair in the same migration boundary.
- [x] Remove the retired root from the local-bundle source inventory.

## Verification

- `python -m pytest -q tests/test_process_liveness.py tests/test_execution_status.py` — 31 passed.
- `python -m pytest -q tests/test_package_local_system.py tests/test_eval_history.py` — 41 passed, 2 skipped.
- `python scripts/check_architecture.py` — passed.
- `python scripts/check_requirement_evidence.py` — passed.
- `git diff --cached --check` — passed.

The full suite was not cleanly re-runnable in this Windows checkout because
elevated pytest processes created workspace temp/cache directories that the
normal runner cannot remove. An earlier full-suite run reached 118 passed and
3 skipped before the first unrelated environment failure; this slice is not
claimed as full-suite qualified here.

This slice does not mark a master-spec requirement complete by itself.
