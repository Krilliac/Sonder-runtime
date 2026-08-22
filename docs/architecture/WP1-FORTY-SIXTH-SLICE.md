# WP1 Forty-Sixth Slice: REPL Duration Presentation Adapter

## Boundary

The interactive REPL now imports its pure elapsed-duration presentation
helper from `sonder_runtime.adapters.observability.repl_formatting`.
`elapsed_label` only normalizes a duration and renders milliseconds or seconds;
it has no session, transport, orchestration, persistence, or command-catalog
coupling. The REPL keeps its private `_elapsed_label` import alias so existing
callers retain the same behavior while the implementation has one canonical
package home.

No server, HTTP interface, command catalog, persistence, launcher, or strangler
compatibility behavior moved in this slice.

## Evidence

- Focused adapter tests: `python -m pytest -q tests/test_repl_formatting.py`
- Compile: `python -m compileall -q sonder_runtime/interfaces/repl/repl.py sonder_runtime/adapters/observability/repl_formatting.py`
- Architecture: `python scripts/check_architecture.py`
- Requirement evidence: `python scripts/check_requirement_evidence.py`
- Diff checks: `git diff --cached --check` and `git diff --check`
