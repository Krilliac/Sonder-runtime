# REMAINING-SEC-003 — OS race-resistant filesystem boundary

The prior archive/path slice validated traversal, links, expansion limits, and
authorized roots but explicitly did not claim check/use race resistance. This
slice adds that missing boundary; the contract module itself never mutates the
filesystem, and a separate POSIX adapter executes delete intents (below).

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

## POSIX executor and its consumer

`sonder_runtime/adapters/filesystem/intent_executor.py` executes a delete
`OpenIntent` (or a `DestructiveTarget` from `check_destructive_targets`). It
opens the authorized root with `O_DIRECTORY | O_NOFOLLOW`, walks every
intermediate component with `os.open(part, O_DIRECTORY | O_NOFOLLOW,
dir_fd=parent)`, inspects the final component with
`os.stat(..., dir_fd=..., follow_symlinks=False)`, and removes it with
`os.unlink(..., dir_fd=...)`. A directory is emptied through descriptors
opened the same way (`os.scandir(fd)`, `os.rmdir(..., dir_fd=...)`), with each
child directory's descriptor identity compared with the `lstat` taken just
before it was opened. A symlink appearing in any component, at the final
name, or inside the tree is refused, never followed or unlinked. A caller may
pass the `lstat` of the checked target; a different object at execution time
is refused. The executor raises `PlatformCapabilityError` on Windows and on any
host where `open`, `stat`, `unlink` or `rmdir` is missing from
`os.supports_dir_fd`, `scandir` is missing from `os.supports_fd`, or
`O_NOFOLLOW`/`O_DIRECTORY` is absent.

Consumer: `file_ops.delete_path` (the `file_delete` tool). After its unchanged
pathname preflight (roots, reparse components, protected and control-plane
paths, the recursive-tree scan, and the `DELETE <path>` confirmation), the
non-developer branch on POSIX builds `build_open_intent(path, roots,
"delete")` and runs the executor, re-checking protected and control-plane
names for every descendant through an entry guard. Refusals surface as
`PermissionError` with the `OSError`/`RaceResistanceError` as the cause; an
unsupported POSIX host raises `PlatformCapabilityError` instead of falling
back to pathname operations.

Not covered: the developer-authorized delete branch and Windows still delete
by pathname (Windows re-checks each entry for reparse points immediately
before removing it, which narrows but does not close the window). Other
mutating operations (write, edit, transfer, batch) do not consume
`OpenIntent`; transfers use the separate parent-directory anchor in
`file_ops._DirectoryAnchor`. The authorized root itself is opened by path and
is the trust anchor.

Evidence: `tests/test_remaining_race_resistance.py` (contract) and
`tests/test_intent_executor_delete.py` (Linux: symlink swapped into an
intermediate component after preflight, symlink and directory rebinding during
a recursive walk, target replacement, in-root-only deletion, and fail-closed
capability checks).
