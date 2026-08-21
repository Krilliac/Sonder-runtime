# Command-catalog canonical packaging evidence

Date: 2026-08-21

This supersedes the earlier import-cycle audit in this file. The command
catalog implementation now lives in `sonder_runtime.adapters.command_catalog`.
The root `command_catalog.py` is an identity redirect to that module, keeping
legacy imports, public/private compatibility surfaces, and monkeypatches
stable.

The packaged adapter preserves lazy access to `server`, `permission_modes`,
and `command_registry`, and source derivation prefers the packaged REPL/HTTP
implementations while retaining a legacy fallback. Reverse-edge root callers
are explicitly listed as reviewed architecture exceptions for this slice.

## Verification

- Expanded focused command-catalog suites: **228 passed**.
- Root identity/private-surface tests: `tests/test_command_catalog_packaging.py`.
- Command-catalog architecture assertions: **5 passed**.
- Compileall: passed.
- Documentation catalog generation/check: passed.
- The standalone architecture checker reports only the pre-existing unrelated
  `web_tools.py` ownership violation; no command-catalog violation remains.

No compaction files or Git metadata were changed by this migration.
