# API-003 packaged native entrypoint safety — 2026-08-21

The supported `python -m sonder_runtime mcp --native` entrypoint now maps an
unsafe-lab startup refusal to a bounded stderr message and exit code `2`
before configuration or application composition. It no longer leaks a Python
traceback when the host is elevated or otherwise fails the mandatory safety
gate.

Evidence:

- `sonder_runtime/__main__.py` catches the typed `UnsafeLabError` at the native
  MCP boundary and refuses before `_load_config`.
- `tests/test_native_mcp.py` verifies refusal ordering, bounded output, and
  exit code.
- `tests/test_api003_subprocess_provider.py`,
  `tests/test_api003_restart_recovery.py`, and
  `tests/test_api003_official_mcp_sdk.py` retain the subprocess, restart, and
  official SDK interoperability coverage.
- Architecture and error-signal gates pass.

The live native provider success path still requires a disposable,
non-elevated host because the safety gate intentionally rejects administrator
execution. This slice does not promote API-003 formally.
