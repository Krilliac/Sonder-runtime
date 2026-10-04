# Resume reality barrier

A checkpoint records what a worker knew. It does not establish that its
workspace is still the workspace that knowledge describes. This barrier is
separate from the [effect journal](REMAINING-AGENT-515-EFFECT-JOURNAL.md): it
does not settle uncertain effects, authorize replay, expand permissions, or
replace the existing owner/revision/provenance checks.

## Observation and delta

`GitWorkspaceReality` records HEAD, branch (`null` for detached HEAD), and a
SHA-256 digest of sorted dirty status/path/size/mtime-nanosecond entries.
Metadata comes from `lstat`; the snapshot does not read or hash file contents.
This is a lightweight observation, not a cryptographic tree attestation:
same-size edits with deliberately preserved timestamps may be indistinguishable.
Git status itself can inspect tracked files internally.

Collection has a shared ten-second deadline, a bounded subprocess output
budget, at most 500 dirty entries, 400 changed paths, and 25 commit summaries.
A complete identical observation produces no delta. Other observations include
history rewrite detection, ahead/behind counts, commits and changed paths
classified as manifest, migration, config, doc or source. Renames include both
paths. Missing commits, incomplete scans, malformed/legacy observations and
Git failures are conservative; unavailable information never counts as an
unchanged tree. An established non-Git workspace has no observation or delta.

Paths and commit subjects are untrusted repository data. The bounded delta is
delivered once to the next request and is not appended to the stable system
prefix. The delta is not a new public tool or SDK contract.

## Persistence and enforcement

* Durable children store host-owned observations inside existing checkpoint
  state, before the state digest and journal provenance are stamped. Runners
  cannot supply the reserved host metadata. Legacy Git checkpoints without
  observations require inspection. Pending requirements survive another save
  and resume. The exact child revision remains pinned through validation and
  resume claim; existing claim behavior retires old verification evidence.
* Interactive lanes use private fields in their existing durable JSON record.
  Resume validates the observed lane revision/attempt before attaching the
  delta. Changed or unavailable workspace inputs bump the existing verifier
  generation, so prior delegated certificates cannot be reused. Private
  fields are excluded from public lane projections.
* Autopilot uses an internal table in its existing database, with writes
  conditional on current ownership. The public run projection is unchanged.
  Existing rows need no migration data. Passed validators and delegated
  certificates are retired on a changed/unknown Git resume, and the store's
  completion gate refuses a pending reality barrier.
* The sealed runtime-checkpoint adapter can be bound to explicit workspace
  roots and a reality port. It seals observations in `repository_state` and
  returns request-only deltas on restore. A changed restore projects an empty
  active `verification` mapping; the original sealed archive remains intact.
  Unbound strategy-observation checkpoints keep their old behavior: those
  records neither declare filesystem roots nor grant mutation/replay authority.

The shared execution barrier is an in-memory check at the typed tool gateway,
direct `journaled_effect` boundary, and legacy effect-class permission gate.
Unbound executions retain their existing decisions and tool risk grades.
These hot paths perform no Git probes. Existing filesystem grants, approvals,
budgets, journal settlement and cancellation checks continue to apply.

## What re-planning means

A changed owned path or rewritten history requires both fresh inspection and
a new plan before mutation. Unknown or truncated information takes the same
conservative path. An absent declared file scope means the entire workspace.

For a durable runner, trusted host code must record a completed inspection,
then call `record_replan` with a non-empty plan digest. Returning model prose or
saving another checkpoint does neither. The composed conversational runner
only invokes models; it receives the delta but has no mutating tool loop.
Future mutating runners must provide their own host-observed acknowledgement.

For an interactive lane, a successful supported workspace-read receipt must
precede a later model response with an explicit plan/rationale. A mutation in
the same response as the read cannot use that read to regain authority. The
one-shot delta prompt states this protocol (read, then a turn beginning
`Plan:`), so a model is not left to guess how to clear a blocked lane. The
terminal snapshot is taken after the lane's waiters and loop turn have been
released, so the Git probe (about 120-160 ms on a loaded Windows workstation,
about 20 ms for a non-Git root) is not on the completion path callers wait on.

Lanes created before this change carry no observation. On their first dispatch
in a Git workspace they are treated as unknown and must pass the same
read-then-`Plan:` sequence once; non-Git workspaces are unaffected.

Autopilot has project-level ownership, so any repository change is in scope.
It runs a read-only inspection under the execution barrier, requires a
`HostTaskResult` with a supported read-tool observation and no observed
mutation, and then requires a schema-checked `replan` result that supersedes
pending work. Existing task and replan ceilings still apply. No receipt,
no replacement plan, or exhausted budget leaves the run paused and the
requirement durable. A receipt establishes that an inspection ran; it does
not attest complete file coverage or the quality of the resulting plan.

These are cooperative trusted-host boundaries, not an in-process Python
sandbox. Arbitrary code in the host process can access its objects and stores.
The barrier is a resume-time observation, not an operating-system lock against
unrelated processes editing the repository after revalidation.

## Local qualification (2026-09-29)

Base: `45a3e093aa423c3a23f9e356419972d2332015e5`, branch
`feat/resume-reality-barrier`. Changes remain uncommitted; no publication or
deployment was performed.

The existing six-suite baseline passed 136 tests before integration. The
broader supported run passed 848 tests across 48 selected regression modules
plus a temporary comparison against the base implementation; seven POSIX
cases skipped. Seventeen cases were excluded after their first attempted run
demonstrated environment constraints: 16 require trusted fixture configuration
outside every writable root, and one requires a Windows named pipe denied by
this sandbox. The normal pytest temporary-directory ACL also fails here; runs
used normal inherited ACLs only for test directories inside this worktree.
Production permission and filesystem guards were not relaxed.

The six direct `journaled_effect` caller families were separately exercised:
135 tests passed and 12 POSIX cases skipped. Five tests failed on artifact-stage
ACLs, including three crash children that never reached their intended crash
cut. Two existing build-fix verifier tests compare a digest of LF text against
fixtures written as CRLF on Windows; the verifier source is identical to the
base, and actual fixture bytes confirmed that mismatch. These are not counted
as passing or as demonstrated crash recovery.

Measured blast radius:

| Surface | Before / after evidence |
|---|---|
| Native and legacy tools | 66 native, 223 legacy, 245 distinct names before and after |
| Risk grades and unbound permission decisions | 0 changed grades; 0 changes in 1,960 mode/interactivity cases compared with the base |
| Permission hot path | 0 Git probes across the tool census, including an unblocked resume barrier |
| Bound native executor census | 47 read-only descriptors remain usable; all 19 other descriptors are refused before executor invocation |
| Production composition | 1 lane constructor, 1 durable-child constructor, 1 runtime-checkpoint constructor; each audited |
| Autopilot | Its single production `execute_run` caller is covered without changing `server.py` |
| Direct effects | All 6 production `journaled_effect` call sites mapped: process start, child dispatch, build-fix edit, compute submit/cancel, self-mod deploy |
| Server size | 26,931 lines before and after; source unchanged |
| Published contracts | No SDK projection, MCP schema, or generated catalog changes |

Six new test modules cover Git identity/deltas, child resume and prompt history,
lane mutation gating and verifier invalidation, runtime-checkpoint compatibility,
Autopilot inspection/replanning, and the complete permission census. They use
temporary repositories, including bare repositories for the established
non-worktree case, because a plain temporary subdirectory of this checkout
would inherit its parent Git repository.
The final run of these modules passed all 58 parametrized cases (52 test
functions); these counts overlap the larger regression runs and are not summed.

Changed files comprise 15 runtime/controller/permission modules, these six
test modules, and three architecture documents. The implementation lives in
`adapters/workspace_reality.py`, `application/ports/workspace_reality.py`,
`application/execution/resume_reality.py`, the child/lane and conversational
runner paths, `adapters/autopilot_reality.py`, the Autopilot controller/store,
runtime-checkpoint adapter/port, the typed and direct effect boundaries,
`permission_modes.py`, and `bootstrap/app.py`.

`check_architecture.py` exited 0; syntax and `git diff --check` passed.
The lint ratchet's product/test-file buckets introduced no findings. Its full
invocation exited 1 because generated scratch fixtures are included in its scan.
Automatic approval review rejected recursive cleanup of both `.resume-work`
and `.workspace-reality-tests` as "blocked by policy"; both scratch directories
remain. No live model, deployment, or hosted CI qualification is claimed.
