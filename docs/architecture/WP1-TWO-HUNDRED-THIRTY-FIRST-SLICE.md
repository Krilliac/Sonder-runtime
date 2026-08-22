# WP1 Two-Hundred-Thirty-First Slice — headless CLI boundary

## Boundary

Moved headless argument parsing and command sequencing into the packaged
`sonder_runtime.interfaces.cli.headless` boundary. The root
`sonder_headless.py` module remains the compatibility composition root: it
owns the supervisor implementations and injects them into the packaged CLI
contract. Existing root helpers, constants, launcher control gating, process
ownership checks, and output contracts remain available. Launcher, logging,
and configuration files are outside this slice.

## Evidence

- `tests/test_headless_cli.py` verifies packaged parser ownership, callback
  sequencing, option propagation, and the legacy stop-Ollama call shape;
  `sonder_headless.main` supplies its historical default host and port.
- `tests/test_headless.py` continues to cover root compatibility behavior,
  safety gating, lifecycle outcomes, and launcher control gating.
- `python -m pytest -q tests/test_headless.py tests/test_headless_cli.py` passes.
- `python scripts/check_architecture.py` passes with zero violations.
- `python scripts/check_requirement_evidence.py` passes.
- `python -m compileall -q sonder_runtime sonder_headless.py` passes.
- `git diff --check` passes.
