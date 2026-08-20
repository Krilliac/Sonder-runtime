# WP1 Thirty-Fourth Slice: Account Rendering Adapter

Status: implemented on `agent/wp1-execution-status`.

## Scope

The stable human-readable account-record renderer moved from the server
composition root to `sonder_runtime.adapters.admin_formatting`. Authentication
and authorization remain owned by the existing admin adapter; only presentation
formatting moved, with the server compatibility symbol preserved.

## Evidence

- Admin/auth, server-helper, and private-COT regressions: **265 passed, 1
  skipped**.
- `python -m compileall -q sonder_runtime server.py`: passes.
- `scripts/check_architecture.py`: passes.
- `scripts/check_requirement_evidence.py`: passes.
- `git diff --cached --check`: passes.

## Remaining boundary

The server still owns the authenticated command handlers and composition logic;
this slice only removes their account presentation helper.
