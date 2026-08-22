# WP1 Two-Hundred-Twenty-Seventh Slice — environment-file policy ownership

## Boundary

Moved environment-file parsing into the packaged
`sonder_runtime.platform.config_environment` policy boundary. The root
`sonder_config.parse_env_file` surface remains an identity-preserving
compatibility wrapper that translates the packaged `EnvironmentFileError` to
the historical `ConfigError` contract. Parsing rules and loader behavior are
unchanged.

This slice is limited to the root configuration compatibility surface and
packaged configuration policy. Client, doctor, server, and bootstrap behavior
are unchanged.

## Evidence

- `tests/test_config_ownership.py` verifies packaged parser ownership and the
  root `ConfigError` compatibility contract.
- `python -m pytest -q tests/test_config_environment_ownership.py tests/test_config_ownership.py tests/production/test_config.py` passes.
- `python scripts/check_architecture.py` passes with zero violations.
- `python scripts/check_requirement_evidence.py` passes.
- `python -m compileall -q sonder_runtime sonder_config.py` passes.
- `git diff --check` passes.
