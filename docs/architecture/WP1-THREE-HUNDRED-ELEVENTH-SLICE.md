# WP1 Three-Hundred-Eleventh Slice — runtime stash formatting

## Boundary

The operator-facing rendering of the runtime source recovery stash
(`_runtime_stash_format`) now lives in
`sonder_runtime/domain/updates/stash_formatting.py` as `format_stash`, with
the status block and the save and pop outcome lines unchanged. `server.py`
keeps `_runtime_stash_format` as an identity-preserving alias import, so the
`/stash` control command calls the same object. The stash operations
themselves remain in `git_tools`.

## Evidence

- `tests/test_stash_formatting_boundary.py` verifies the alias identity, the status block without path echo, and the save, save-untracked and pop outcome lines.
- `python -m pytest -q tests/test_stash_formatting_boundary.py tests/test_git_tools.py -k 'stash or boundary'`
- `python scripts/check_architecture.py`
- `python scripts/check_error_signals.py`
- `python -m compileall -q sonder_runtime server.py`
- `git diff --check`
