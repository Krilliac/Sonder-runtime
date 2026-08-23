# Selfmod continuous loop stuck, noisy, or not producing results

`scripts/selfmod_forever.py` (started via `scripts/start-selfmod.ps1`) drives
the `selfmod` self-improvement lifecycle unattended for a bounded number of
hours, one candidate at a time, committing each accepted result to its own
`selfmod/<run-id>` branch. It never writes the checked-out working tree. See
[SELFMOD.md](../../SELFMOD.md#continuous-unattended-loop) for the lifecycle
and safety properties; this runbook is the operational side — starting,
checking on, and unsticking it.

## Check whether it is running

```powershell
powershell -NoProfile -File scripts\start-selfmod.ps1 -Status
```

This reports the PID (from `Win32_Process` filtered on `selfmod_forever` in
its command line) and the last 12 lines of
`%LOCALAPPDATA%\sonder\selfmod-continuous.log`. Tail the full log for detail:

```powershell
Get-Content "$env:LOCALAPPDATA\sonder\selfmod-continuous.log" -Tail 80 -Wait
```

## It refused to start

`start-selfmod.ps1` fails fast, before spawning anything, on three conditions
— fix the one it names and rerun:

- **"a selfmod loop is already running"** — only one loop may run at a time
  (two running concurrently once raced on the same run state and deployment
  lock). Run `-Status` to see the PID, or `-Stop` to end it first.
- **"working tree is dirty (N path(s))"** — every pass commits inside an
  isolated worktree, but `selfmod`'s own deploy path refuses to commit when
  the *source* checkout started dirty, so a loop launched here could not
  produce anything reviewable. Commit or stash your changes first.
- **"no Python interpreter found"** — pass `-Python <path>` or set
  `$env:SONDER_PYTHON`; the script otherwise looks for
  `venv\Scripts\python.exe` under the repo root, then `python` on `PATH`.

## It stopped itself early

The log's closing lines say why:

- `"stopped early: N consecutive passes produced no commit"` — hit
  `-MaxBarren` (default `6`). This is not necessarily a problem: it is the
  intended behavior when the model is down or has run out of distinct,
  actionable proposals against the current `CANDIDATE_FILES` list in
  `scripts/nightly_selfmod.py`. Read the last several pass lines in the log —
  every rejection reason (`candidate rejected: ...`) is logged — before
  assuming the backend is broken. Rerun with `-MaxBarren 0` only once you've
  confirmed passes are failing for a real, fixable reason; otherwise you are
  just burning GPU time on an exhausted file set.
- `"stopping: the tree must be clean for a run to be committable"` — the
  source checkout went dirty *while the loop was running* (a concurrent
  session edited files). The loop will not fix this itself; clean the tree
  and restart.
- The loop reached its `-Hours` deadline — this is normal completion, not a
  failure. Every commit made is listed at the end of the log and via:

  ```bash
  git log --oneline --all --grep='^selfmod:'
  ```

## It is still running but nothing has committed

Check the log for repeated `"model unavailable"` (the local model backend is
down or the pinned `-Model` tag isn't installed) versus repeated
`"candidate rejected: ..."` with varying reasons (backend is fine; the model's
proposals keep failing review). The first is an Ollama/model problem — see
[ollama-outage.md](ollama-outage.md). The second is expected some fraction of
the time; the loop is designed to reject more candidates than it keeps.

## Orphaned worktrees and branches after a kill

Before this loop's orphan-reclaim step existed, killing it mid-pass (the
common way earlier runs ended) left behind an `editing`/`testing` run with no
owner, its ~100 MB Git worktree, and its branch — invisible to
`selfmod.prune_workspaces()`, which is retention-based and skips active
phases. The loop now reclaims these automatically at startup (any run in
`editing`/`testing`/`backed_up`/`proposed` with no owner — only a run this
loop itself claimed counts as owned — is cancelled and its worktree/branch
discarded), so this should be rare going forward. `/selfmod history` and
`/selfmod inspect <run-id>` show the phase but not ownership; a run sitting in
`editing`/`testing` with no corresponding activity in the continuous-loop log
is the visible symptom. If you still find leftovers — e.g. after
force-killing the Python process itself rather than using `-Stop`:

```bash
git worktree list                       # find selfmod worktrees under
                                         #   <SONDER_HOME>/selfmod/workspaces/
git worktree remove --force <path>
git worktree prune
git branch -D selfmod/<run-id>          # only for runs you don't intend to review
```

Confirm a run is truly abandoned before deleting anything: `/selfmod inspect
<run-id>` shows the phase but not its last-updated time, so cross-check
against the continuous-loop log (is *any* loop still emitting pass lines?)
and, for a run older than a few hours in `editing`/`testing` with no matching
loop running, treat it as abandoned. A run belonging to a *different*,
still-running loop looks the same at a glance, and only one loop is supposed
to run at a time.

## Reviewing and disposing of results

Every accepted candidate is a normal branch, not a merge and not a deploy:

```bash
git log -p selfmod/<run-id>             # review the change
git cherry-pick <sha>                   # take it, on whatever branch you're on
git branch -D selfmod/<run-id>          # or discard it
```

Nothing under `selfmod/*` is pushed, merged, or rebased by the loop itself.
