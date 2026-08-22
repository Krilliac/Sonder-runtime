# UPDATE-002/004 durable activation evidence

This bounded slice connects the existing platform-neutral `ActivationRequest`
and `PlatformActivationHelper` contracts to `DurableActivationCoordinator`.

The coordinator verifies signed release evidence and exact sealed runtime
dependencies before invoking the helper. It journals `prepared` before
platform execution and `activated`, `recovered`, or `recovery_failed` after
the outcome. On activation or pointer-commit failure it invokes helper
rollback and restores the known-good pointer; incomplete recovery raises
`ActivationRecoveryError` rather than being reported as success.

`JsonActivationJournal` is the adapter used by the focused tests. The
coordinator constructs no shell commands or platform paths; malformed helper
and pointer ports fail closed at composition.

Verification: `python -m pytest -q --basetemp .pytest-update002
tests/test_update002_004_durable_activation.py` — **3 passed in 1.64s**.
`compileall` and `git diff --check` also passed for the slice.
