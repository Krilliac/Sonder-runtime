# WP1 Thirty-Second Slice: Command Parser Adapter

Status: implemented on `agent/wp1-execution-status`.

## Scope

The pure game-campaign command parser moved from the server composition root to
`sonder_runtime.adapters.command_parsing`. The server keeps its compatibility
symbol and retains timeout parsing locally because that parser still depends on
the grounding boundary; no new root import was introduced.

## Evidence

- Server-helper and game-forge regressions: **234 passed**.
- `python -m compileall -q sonder_runtime server.py`: passes.
- `scripts/check_architecture.py`: passes.
- `scripts/check_requirement_evidence.py`: passes.
- `git diff --cached --check`: passes.

## Remaining boundary

The server composition root still owns transport orchestration and several
parsers coupled to current root modules. Future extractions must either move
those dependencies first or retain them locally until their canonical adapter
boundary exists.
