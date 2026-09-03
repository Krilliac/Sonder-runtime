# WP1 Three-Hundred-Forty-Sixth Slice — agent help text parsing

## Boundary

Moved `_agent_help_advertised_tools` from server.py into
`sonder_runtime/domain/agent_help_parsing.py` as `help_advertised_tools`.

Extracts tool names from agent help text blocks by parsing lines matching
`- name: {…}`, filtering to valid Python identifiers. Pure string parsing,
stdlib only.

The root `server._agent_help_advertised_tools` is an identity-preserving
alias.

## Evidence

- `tests/test_agent_help_parsing_boundary.py` verifies identity alias,
  tool name extraction, non-tool line filtering, colon requirement,
  non-identifier rejection, empty/None input, and return type.
- `python -m pytest -q tests/test_agent_help_parsing_boundary.py` — 7 passed
- `python scripts/check_architecture.py` — silent, exit 0
- `python scripts/check_requirement_evidence.py` — silent, exit 0
- `python -m compileall -q sonder_runtime/domain/agent_help_parsing.py server.py` — silent, exit 0
- `git diff --check` — silent, exit 0
