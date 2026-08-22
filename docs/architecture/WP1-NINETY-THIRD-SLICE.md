# WP1 Ninety-Third Slice: packaged entrypoint version boundary

## Change

`sonder_runtime.__main__` now imports build metadata through
`sonder_runtime.platform.version` instead of importing the root
`sonder_version` module directly. The entrypoint's status, diagnostics, and
MCP version reporting continue to use the same `BuildInfo` and `build_info`
objects, so release metadata and compatibility behavior are unchanged.

## Scope

This slice changes only the packaged entrypoint's version dependency and its
focused regression coverage. The root `sonder_version.py` implementation and
compatibility façade remain in place because release tooling and packaging
still require the literal version metadata there.

## Evidence

- `tests/test_runtime_entrypoint_version_boundary.py`
- `python -m pytest -q tests/test_runtime_entrypoint_version_boundary.py`
- `python -m compileall -q sonder_runtime/__main__.py sonder_runtime/platform/version.py`
- `python scripts/check_architecture.py`
- `python scripts/check_requirement_evidence.py`
- `git diff --cached --check`
- `git diff --check`
