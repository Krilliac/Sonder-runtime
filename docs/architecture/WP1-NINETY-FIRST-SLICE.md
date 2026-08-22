# WP1 Ninety-First Slice: workbench path seam

`sonder_runtime.adapters.filesystem.workbench` now resolves shell runners
through `sonder_runtime.platform.paths` instead of importing the root
`sonder_paths` module directly.

The change preserves execution behavior: `run_program` and `run_script` still
use the same platform-selected Bash executable, while workspace resolution,
containment checks, argv-only execution, and script validation remain owned by
the existing filesystem adapters.

## Evidence

- `tests/test_workbench_paths_boundary.py`
- `python -m pytest -q tests/test_workbench_paths_boundary.py tests/test_workbench.py tests/test_workbench_inline_shell.py tests/test_workbench_server.py`
- `python -m compileall -q sonder_runtime server.py`
- `python scripts/check_architecture.py`
- `python scripts/check_requirement_evidence.py`
- `git diff --cached --check`
- `git diff --check`
