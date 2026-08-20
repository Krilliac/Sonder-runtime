# WP1 Fortieth Slice: Catalog and Packaged Entrypoint Boundary

Status: implemented on `agent/wp1-execution-status`.

## Scope

The command catalog now discovers slash-command sources from the packaged REPL
and HTTP interfaces, with legacy-root fallback for older checkouts. The
package-local-system manifest and slash-command probe now point at the
canonical packaged HTTP interface; tests and source documentation no longer
require the retired root `sonder_serve.py` implementation.

## Evidence

- Command-catalog, package-manifest, model-inventory, and related regression
  tests: **83 passed, 2 skipped**.
- `python -m compileall -q sonder_runtime server.py`: passes.
- `scripts/check_architecture.py`: passes.
- `scripts/check_requirement_evidence.py`: passes.
- `git diff --cached --check`: passes.

## Remaining boundary

The root `server.py` composition boundary and immutable migration compatibility
aliases remain. The catalog now follows the packaged interface ownership while
retaining compatibility for old layouts.
