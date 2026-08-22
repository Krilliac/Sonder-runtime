# WP1 One-Hundred-Seventh Slice — Remove the version root allowance

Runtime implementation ownership for build identity now belongs to
`sonder_runtime.platform.version`, and the persistence migration registry's
last packaged caller was migrated in the One-Hundred-Third Slice. A bounded
source audit found no `sonder_runtime` production module importing the root
`sonder_version` module.

This slice removes only `sonder_version` from `ROOT_PLATFORM_MODULES`. The
root `sonder_version.py` compatibility surface and its literal `VERSION`
assignment remain unchanged for release tooling, and `unsafe_lab` remains an
explicitly separate architecture policy boundary.

Regression coverage asserts both the removed allowance and the absence of
root-version imports under `sonder_runtime`. Evidence is provided by
`tests/production/test_architecture.py`, the release-policy/version-boundary
tests, and the compile, architecture, requirement-evidence, and diff gates.
