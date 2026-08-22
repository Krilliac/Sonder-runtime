# WP1 live-reload root retirement evidence

Date: 2026-08-21

## Decision

`sonder_runtime/adapters/web/live_reload.py` is the canonical owner of the
live-reload implementation. The repository-root `live_reload.py` compatibility
redirect has been retired and removed.

## Safety evidence

- The packaged adapter retains the public surface of `enabled`, `prime_modules`,
  `reload_changed_modules`, and `snapshot`.
- Production imports in `server.py`, `self_heal.py`, and the HTTP/REPL
  interfaces already target the packaged adapter.
- `tests/test_live_reload.py::test_packaged_live_reload_owns_behavior_after_root_retirement`
  proves the root module is absent and the packaged adapter owns the API; the
  remaining tests cover source edit,
  priming, disablement, failed reload rollback, symbol removal, state
  preservation, importer rebinding, and server lifecycle rebinding.

## Verification

Focused command: `python -m pytest -q tests/test_live_reload.py --basetemp
.pytest-tmp-live-reload-final`

Final result: **20 passed in 1.71s**.

Repository gates after retirement: architecture, requirement evidence,
evidence documents, documentation authority/catalog, compileall, and diff
check all passed. The diff check emitted only the repository's normal
autocrlf normalization warnings.
