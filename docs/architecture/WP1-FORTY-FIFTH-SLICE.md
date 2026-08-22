# WP1 Forty-Fifth Slice: Chat Usage Presentation Adapter

## Boundary

The HTTP interface now delegates OpenAI-compatible token-usage shaping to
`sonder_runtime.adapters.observability.chat_formatting.chat_usage`.
The helper is pure presentation logic: it accepts an activity-shaped mapping,
normalizes bounded integer token counts, and returns the stable usage object
used by both complete and streaming chat responses.

No HTTP transport, server state, persistence, command catalog, launcher, or
strangler compatibility behavior moved in this slice.

## Evidence

- Focused adapter tests: `python -m pytest -q tests/test_chat_formatting.py`
- Compile: `python -m compileall -q sonder_runtime/interfaces/http/serve.py sonder_runtime/adapters/observability/chat_formatting.py`
- Architecture: `python scripts/check_architecture.py`
- Requirement evidence: `python scripts/check_requirement_evidence.py`
- Diff checks: `git diff --cached --check` and `git diff --check`
