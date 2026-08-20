# WP1 Twenty-First Slice: Package Guarded Filesystem Operations

Status: implemented on `agent/wp1-execution-status`.

## Scope

The guarded filesystem implementation now lives at
`sonder_runtime.adapters.filesystem.file_ops`. Root callers, server live
reload, the inspection executor, strangler tool adapter, and filesystem-related
tests use the package-qualified module. Root `file_ops.py` is retired.

The architecture policy contains only narrow exceptions for the filesystem
adapter's optional YAML parser and its direct git-discovery dependency; no
general third-party or adapter-cycle allowance was added.

## Evidence

- File operations, sensitive-read, batch-write, transfer, containment,
  workbench, archive, control-plane, inspection, and production architecture
  regression: **325 passed, 26 skipped**.
- `scripts/check_architecture.py`: passes with the root legacy ratchet reduced
  to 12.
- `scripts/check_requirement_evidence.py`: passes.
- `git diff --cached --check`: passes.

## Remaining boundary

The root workbench and serving/lifecycle/update entrypoints remain explicit
legacy boundaries. The filesystem implementation itself no longer depends on
the retired root name.
