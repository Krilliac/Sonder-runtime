# WP1 Two-Hundred-Seventy-Second Slice — fanout cloud-policy migration

## Boundary

Rewired fanout plan admission, durable run creation, and execution-time cloud
revocation fences to invoke the packaged cloud opt-in policy directly. The
immutable receipt opt-in marker and pre-dispatch safety checks remain intact.

## Evidence

- AST regression tests prove `_fanout_plan`, `_fanout_start`, and
  `_execute_fanout_run` contain no call to the root `cloud_allowed()` wrapper.
- Fanout, cloud-routing, and request-cache regressions pass: **231 passed**.
- `server.py` compiles; `git diff --check` and the architecture gate pass.

## Limitation

Serve-target, gateway context injection, REPL, and remaining reporting cloud
callers remain staged. MCP parity, epoch-2 bridge retirement, and formal
checklist acceptance remain incomplete.
