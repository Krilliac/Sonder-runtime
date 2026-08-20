# WP1 One-Hundred-First Slice — Canonical version implementation

This slice moves `BuildInfo`, build-stamp loading, and source-checkout Git
identity into `sonder_runtime.platform.version`. The root `sonder_version.py`
now retains only the literal `VERSION` required by AST-based release tooling
and compatibility exports that resolve to the canonical module.

The stamp location remains the package root (`sonder_build.json`), matching
`scripts/package_local_system.py`. Runtime callers already consume the
packaged platform boundary, so their version and build metadata are unchanged.

The `sonder_version` platform allowance remains necessary for the immutable
migrations adapter, which is intentionally outside this slice. Removing that
last root import requires a separate persistence migration and is not silently
performed here.

The architecture checker permits `subprocess` only for the existing update
engine identity path and this canonical version boundary, because the latter
must preserve the source-checkout Git revision fallback.

Evidence: `tests/test_version_implementation_boundary.py`, the focused version,
release-policy, package, update, lifecycle, backup, and entrypoint suites, plus
compile, architecture, requirement-evidence, and diff checks.
