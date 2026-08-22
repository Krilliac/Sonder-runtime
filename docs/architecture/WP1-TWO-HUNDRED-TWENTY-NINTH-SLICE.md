# WP1 Two-Hundred-Twenty-Ninth Slice — HTTP path-boundary migration

## Boundary

Migrated the packaged HTTP interface's default-home and local server-log path
resolution to `sonder_runtime.platform.paths`. The root `sonder_paths` module
remains an identity-preserving compatibility alias, so legacy callers and
monkeypatch surfaces remain unchanged. This slice is limited to the packaged
HTTP path seam; configuration and system-profile ownership are unchanged.

## Evidence

- `tests/test_http_serve_paths_boundary.py` verifies the packaged import seam
  and local server-log resolution through the canonical path module.
- `tests/test_paths_ownership.py` continues to verify root/package module
  identity and public helper aliases.
- `python -m pytest -q tests/test_http_serve_paths_boundary.py tests/test_paths_ownership.py` passes.
- `python scripts/check_architecture.py` passes with zero violations.
- `python scripts/check_requirement_evidence.py` passes.
- `python -m compileall -q sonder_runtime sonder_paths.py` passes.
- `git diff --check` passes.
