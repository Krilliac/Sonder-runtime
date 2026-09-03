# WP1 Three-Hundred-Twenty-Eighth Slice — repo-repair pytest runner

## Boundary

The bounded pytest run for one scratch repo-repair project
(`_repo_repair_pytest`) now lives in
`sonder_runtime/adapters/repo_repair_runner.py` as `run_pytest`, with the
detached stdin, the child environment, the timeout and spawn handling and
the exit-code attribution rules unchanged. It spawns a child process, so the
adapters layer is its home. `server.py` keeps `_repo_repair_pytest` as an
identity-preserving alias import, so the repo-repair loop calls the same
object.

## Evidence

- `tests/test_repo_repair_runner_boundary.py` verifies the alias identity, attributable passing and failing candidates, and a timeout reported as infrastructure rather than a verdict.
- `python -m pytest -q tests/test_repo_repair_runner_boundary.py tests/test_repo_repair.py`
- `python scripts/check_architecture.py`
- `python scripts/check_error_signals.py`
- `python -m compileall -q sonder_runtime server.py`
- `git diff --check`
