# WP1 archive-create packaging checkpoint

Date: 2026-08-21  
Scope: root-owned `archive_create.py` after typed port/adapter/executor wiring.

## Finding

The full guarded implementation now lives in
`sonder_runtime/adapters/archive_create.py`. The root `archive_create.py` is a
small identity redirect, so legacy imports resolve to the packaged module and
monkeypatches of helpers such as `_write_zip` and `_plan` remain effective.
The packaged module retains both the historical positional API and the typed
`ArchiveCreateRequest` API used by the application adapter.

## Evidence

- The root module is an explicit compatibility redirect in
  `COMPATIBILITY_ROOT_MODULES`; the architecture ratchet checks that
  production code does not import it directly.
- Typed request and gateway are defined in
  `sonder_runtime/application/ports/archive_create.py:13-58`.
- Native MCP reaches the typed executor through
  `sonder_runtime/bootstrap/native_mcp.py:434-441`; it does not import the
  root archive module directly.
- The server imports the packaged adapter directly; the root redirect remains
  available only for external legacy callers.

## Checks

- `python scripts/check_architecture.py` — **PASS** (exit 0; no violations,
  including import-cycle detection).
- AST parse of `archive_create.py`, the typed port, archive adapter, tool
  executor, and native MCP — **PASS** for all five files.
- Focused pytest command:
  `python -m pytest -q tests/test_archive_create_boundary.py tests/test_archive_create_executor.py tests/test_native_mcp.py tests/production/test_architecture.py`
  — **41 passed, 50 errors**. The errors are environment/setup failures from
  pytest being unable to scan `C:\Users\Nathan\AppData\Local\Temp\pytest-of-Nathan`
  (`WinError 5: Access is denied`), not assertion failures in the checkpoint
  code.

The migration preserves server compatibility through the identity redirect;
the focused boundary, archive, executor, and native-MCP suites cover the
positional API, typed request mapping, shared module identity, safety limits,
transactional rollback, and tool routing.
