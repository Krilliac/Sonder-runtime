# WP1 Thirtieth Slice: Response Formatting Adapter

Status: implemented on `agent/wp1-execution-status`.

## Scope

The server composition root no longer owns response footer, interaction-ID,
or observable-activity formatting. Those functions now have one canonical
implementation in `sonder_runtime.adapters.observability.response_formatting`.
The server retains compatibility symbols for existing HTTP, REPL, and direct
callers while delegating to the adapter.

## Evidence

- Server helper regression: **216 passed**.
- `python -m compileall -q sonder_runtime server.py`: passes.
- `scripts/check_architecture.py`: passes.
- `scripts/check_requirement_evidence.py`: passes.
- `git diff --cached --check`: passes.

## Remaining boundary

The server remains the large composition root; this slice removes one bounded
implementation group without changing transport behavior. Further server
responsibilities must be extracted behind the same dependency-direction and
focused-regression gates.
