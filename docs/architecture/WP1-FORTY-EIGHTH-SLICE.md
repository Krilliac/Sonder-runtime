# WP1 Forty-Eighth Slice: Command Completion Limit Adapter

## Boundary

The HTTP interface now imports its pure completion-limit normalization from
`sonder_runtime.adapters.command_completion`. The adapter only converts the
optional query value to an integer and clamps it to the documented range; it
has no HTTP transport, command catalog, persistence, launcher, or orchestration
coupling. The HTTP module retains its public constants and private helper name
as compatibility aliases while the implementation has one canonical package
home.

No server, REPL, command catalog, persistence, launcher, or strangler behavior
moved in this slice.

## Evidence

- Focused adapter tests: `python -m pytest -q tests/test_command_completion.py tests/test_serve_commands.py`
- Compile: `python -m compileall -q sonder_runtime/interfaces/http/serve.py sonder_runtime/adapters/command_completion.py`
- Architecture: `python scripts/check_architecture.py`
- Requirement evidence: `python scripts/check_requirement_evidence.py`
- Diff checks: `git diff --cached --check` and `git diff --check`
