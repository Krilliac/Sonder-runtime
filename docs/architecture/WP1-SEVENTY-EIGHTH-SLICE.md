# WP1 Seventy-Eighth Slice: NPU manifest path seam

The packaged NPU manifest adapter now resolves its per-user manifest directory
through `sonder_runtime.platform.paths` instead of importing the root
`sonder_paths` module directly.

This is a behavior-preserving boundary move: the platform seam still delegates
to the existing path implementation, while the NPU adapter depends only on the
packaged path contract. Manifest validation, hashing, file verification, and
environment-variable overrides are unchanged.

## Evidence

- `tests/test_npu_manifest.py::test_manifest_dir_uses_packaged_platform_paths`
- NPU manifest regression tests
- `python -m compileall -q sonder_runtime server.py`
- `python scripts/check_architecture.py`
- `python scripts/check_requirement_evidence.py`
- `git diff --cached --check`
- `git diff --check`
