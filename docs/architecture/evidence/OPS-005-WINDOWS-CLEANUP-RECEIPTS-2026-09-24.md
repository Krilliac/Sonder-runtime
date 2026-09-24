# Windows cleanup receipts

The raw Windows supervisor previously reported full process-tree cleanup when
`taskkill /PID ... /T /F` returned zero. That command result supplies no retained
containment handle or independent proof that all owned descendants are gone.
It now reports an incomplete receipt even on command success. Errors and timeouts
remain incomplete, and the invocation keeps its existing finite timeout.

Recorded cancellation also takes precedence during deadline recovery. A retry
after the root exits re-enters cancellation; it cannot silently discard pending
cleanup by marking the job interrupted solely from a root liveness observation.
Existing process-identity checks remain in the cleanup adapter.

This is an intentional compatibility correction. Raw Windows jobs keep their
cancellation request, resources and retry timer until cleanup is proven. Raw
registry recovery returns an incomplete receipt and does not claim the orphan
tree was cleaned. MCP close retains its incomplete receipt and lifecycle event
after direct-child reaping; MCP does not schedule automatic cleanup retries.

The existing native Windows Job Object path remains the completion path for
contained jobs. It observes job accounting and retained process handles before
releasing its containment token. No PID snapshot is substituted for this proof.

Local qualification:

- Native supervisor/provider/MCP/service/containment cohort: 124 passed, three
  explicit POSIX-only skips. Separate native registry restart rehearsal: one
  passed. The containment cohort includes real parent/child Job Object cleanup
  and checks that all retained handles are signaled and job accounting is zero.
- Zero-return taskkill cannot release provider capacity or become a complete
  MCP receipt after the direct child is reaped. Native deadline cancellation
  succeeds through Job Objects; raw restarted cleanup remains unresolved.
- Native CI now includes the supervisor, MCP and registry restart regressions
  and the real Windows containment cohort as required tests.

OPS-005 remains implemented_unverified. Raw Windows tree absence, containment
restoration after service restart, escaped descendants, live POSIX group-kill
completion and the larger held identity-propagation patch remain unqualified.
No production deployment or broader requirement completion is claimed.
