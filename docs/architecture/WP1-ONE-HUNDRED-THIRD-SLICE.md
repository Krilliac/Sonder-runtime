# WP1 One-Hundred-Third Slice — migration release-version boundary

`sonder_runtime.adapters.persistence.migrations` now consumes build identity
from `sonder_runtime.platform.version`, completing the packaged caller
migration after the version implementation moved there. The root
`sonder_version` compatibility surface remains unchanged for release tooling
and legacy imports.

Focused regression coverage proves that migration replay still records the
same release version and SHA-256 checksum, remains idempotent, and does not
modify the immutable repository migration bytes. No migration file was
rewritten.
