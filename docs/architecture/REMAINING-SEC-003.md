# REMAINING-SEC-003 — OS race-resistant filesystem boundary

The prior archive/path slice validated traversal, links, expansion limits, and
authorized roots but explicitly did not claim check/use race resistance. This
slice adds that missing boundary without mutating the filesystem.

`race_resistant_paths.py` reports the primitives available from the running
Python process. On Windows it fails closed because ordinary Python pathname
operations do not prove reparse-safe `CreateFileW` handle semantics. On POSIX,
destructive intents require directory-descriptor support and `O_NOFOLLOW`.
Every candidate is checked for symlink/reparse components and resolved twice;
`build_open_intent` carries the required no-follow flags and directory-handle
requirement for a native adapter to execute. It does not open, delete, create,
or replace anything itself.

Destructive target batches are bounded by count, path length, and depth; root
deletion, missing targets, duplicate targets, outside-root paths, and unsafe
components are rejected before the capability decision. Unsupported platforms
raise `PlatformCapabilityError` instead of silently falling back to an ordinary
pathname operation.

Evidence: `tests/test_remaining_race_resistance.py`.
