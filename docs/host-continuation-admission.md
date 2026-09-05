# Managed parent admission composition

`HostContinuationAdmission` accepts an already authenticated bound continuation
and the fresh host context. It intersects workspace roots, retains the original
deadline ceiling, and preserves the fresh principal, source, and cancellation
object. The bound continuation validates that identity before use.

Each `invoke` checks live attachment authority and the complete model-root and
private-store inventories before passing its immutable context to one operation.
Inventories must be host-owned, nonempty, bounded collections of absolute paths.
Both lexical paths and resolved aliases participate in overlap checks. A newly
relocated private store or expanded global model root therefore refuses the next
operation even when the parent's own workspace remains narrow.

This is a composition dependency, not an enabled recovery endpoint. The launcher
must still supply authenticated host selection, inventory every private database
and credential directory, and call admission at each model/reviewer/tool boundary.
Transactional effect admission, anchored persistence, and file-tool control-plane
exclusions remain necessary. A check before dispatch is not an OS sandbox and
does not revoke arbitrary subprocess access by the same operating-system user.

REPL persisted session selection can provide the host binding. App chat labels,
MCP connection identifiers, model arguments, and old parent tokens cannot.
