# Safe self-improvement in Sonder Runtime

Sonder Runtime self-improvement is a host-controlled state machine. Sonder is
not the candidate or base model: candidate model output is only one untrusted
input to the runtime. Candidate output can inspect and edit only an isolated
workspace; it cannot approve itself, decide that tests passed, deploy source,
edit backups, or invoke another self-improvement run.

## Trust boundaries and phases

The persistent phases are:

```text
observed -> proposed -> backed_up -> editing -> testing -> reviewing
         -> approved -> deployed

editing/testing/reviewing -> rejected -> restored
deployed -> rollback_requested -> restored
```

`selfmod.py` owns SQLite state, immutable backups, hashes, budgets, test
evidence, acceptance, deployment locks, and restoration. `server.py` may ask the
existing guarded workbench agent to edit the isolated workspace, but the host
independently inventories its complete diff and runs deterministic commands.

Git checkouts use a detached worktree and dedicated `selfmod/<run-id>` branch.
The starting commit and exact dirty status are recorded. Unrelated dirty work is
never stashed, reset, cleaned, or committed. Non-Git installations receive a
full isolated snapshot workspace. Both modes additionally require file-level
backup bundles.

## Modes

- `observe`: inspections and opportunities only; no backup/edit lifecycle.
- `propose` (default): may build and test an isolated candidate, but deployment
  requires explicit approval.
- `auto-low-risk`: deterministic host checks may approve only narrowly scoped
  low-risk work. High/critical and protected work always requires a user.

Disable all self-modification with `/selfmod disable`. Re-enable explicitly
with `/selfmod enable`.

## Backup and audit storage

State is stored under the per-user Sonder Runtime directory:

```text
<SONDER_HOME>/selfmod/selfmod.db
<SONDER_HOME>/selfmod/backups/<run-id>/manifest.json
<SONDER_HOME>/selfmod/backups/<run-id>/manifest.sha256
<SONDER_HOME>/selfmod/backups/<run-id>/files/...
<SONDER_HOME>/selfmod/workspaces/<run-id>/...
```

`SONDER_SELFMOD_HOME` and `SONDER_SELFMOD_DB` can relocate state, but the
state directory must remain outside the editable repository. Each manifest
records the absolute repository, starting commit, existence, SHA-256, size,
mode, backup path, and backup hash for every authorized file. New files are
recorded with `existed_before=false`. Backups are hash-verified before editing,
deployment, rehearsal, and restoration. Corruption fails closed.

The SQLite `selfmod_events` table is append-only through the public API and
records proposals, backups, edits, diffs, tests, reviews, approvals, locks,
deployments, health checks, and rollback. Retention is age/size bounded and
never deletes the newest valid rollback bundle.

## Protected policy

Automatic edits cannot touch approval, backup, rollback, permission, account,
credential, audit, evaluator, security-test, deployment, or restart-critical
control-plane files. The canonical policy is returned by
`selfmod.protected_paths()`. Protected maintenance requires both an explicitly
authorized maintenance run and explicit user approval; `auto-low-risk` cannot
approve it.

Acceptance also rejects:

- any candidate file outside the pre-backed-up scope;
- removed/renamed tests from the pre-change inventory;
- missing or failed syntax, targeted, regression, or smoke checks;
  - `syntax` compiles the declared Python modules that survive in the candidate.
    If every declared module was deleted, or the run declares no Python at all,
    that is a refusal: an empty target set is not a pass.
  - `smoke` imports the candidate's own modules in a child process rooted at the
    workspace, confirms any declared deletion is genuinely unreachable, and must
    return a SHA-256 receipt over the bytes it loaded. The receipt is computed by
    the recording process and never handed to the probe, so exit 0 on its own
    cannot satisfy the gate.
- oversized diffs or file counts;
- source conflicts after planning;
- corrupted backups or failed rollback rehearsal.

## Commands

```text
/selfmod status
/selfmod opportunities
/selfmod history
/selfmod inspect <run-id>
/selfmod plan <objective> --files module.py,tests/test_module.py
/selfmod plan <objective> --maintenance --files protected.py,tests/test_security.py
/selfmod run <objective> --files module.py,tests/test_module.py --tests python -m pytest -q tests/test_module.py
/selfmod run <protected objective> --maintenance --files ... --tests <reproducer> ;; <security-suite>
/selfmod diff <run-id>
/selfmod tests <run-id>
/selfmod approve <run-id>
/selfmod reject <run-id> [reason]
/selfmod deploy <run-id>
/selfmod rollback <run-id>
/selfmod backups
/selfmod verify-backup <run-id>
/selfmod mode observe|propose|auto-low-risk
/selfmod resume <run-id>
/selfmod cancel <run-id>
/selfmod disable
/selfmod retention <days> <max-gb>
/selfmod prune-backups
```

The hosted chat API accepts the same slash lifecycle with developer/admin
authorization for mutating actions. `/v1/sonder/status` exposes the current
mode, phase summaries, active runs, backup root, and rollback-point count for
the Flutter System page.

## Deployment and crashes

Before deployment, the host re-verifies backups, the complete candidate diff,
the starting commit, dirty-tree fingerprint, scope, inventory, and approval.
Files are replaced with same-directory temporary files plus `fsync` and
`os.replace`. Deletions happen only for declared files whose candidate version
was removed.

Deployment then verifies, first and unconditionally, that the code just written
can still undo itself. A child process running the *deployed* bytes dry-runs
both rollback routes against temporary directories: the in-tree manifest
restore that `/selfmod rollback` funnels through, and the out-of-tree
`selfmod_recover` entry point driven through its own manifest bundle, so its
checksum gate and path confinement execute too. Both must return the exact
pre-deploy bytes and agree on a content receipt whose expected value is
computed by the deploying process and deliberately never given to the child.
This runs whether or not a caller supplies a health command, because the
unattended nightly lane supplies none, and because a `--maintenance` run is
allowed to rewrite the recovery path itself. It writes only inside its
temporary directories, so it can fail without changing anything.

A separate Python health subprocess then imports Sonder Runtime and requests
status. That subprocess proves the new bytes import; it is not, and never was,
evidence that rollback still works.

Failure of either automatically restores exact backup hashes. Already-loaded
helper modules on Sonder Runtime's conservative live-reload allowlist are then
reloaded; a reload error also triggers rollback. Restart-critical supervisor,
server, ledger, and recovery modules are protected maintenance targets, so the
running process never replaces its own recovery/control path automatically.

Deployment records the exact post-deploy hash or absence of every changed
path. A later manual rollback refuses to overwrite a file that the user changed
after deployment; it reports a conflict and preserves the current bytes for
explicit resolution. Immediate health-check recovery still restores the
verified pre-deploy bundle automatically.

Only one deployment, rollback, or crash recovery can hold the cross-process
SQLite lease. A live local owner cannot lose its lock solely because a lease
timestamp expires; crash recovery atomically takes ownership and holds the
global lock through exact restoration. Editing/testing runs with stale owners become
`interrupted`; they never resume without `/selfmod resume <run-id>`.

No command pushes, fetches, rebases, resets, cleans, installs dependencies, or
rewrites Git history. A clean Git checkout receives a separate descriptive
selfmod commit. A checkout that was already dirty is deployed without making a
commit so unrelated staged/unstaged user work is untouched.

## Continuous unattended loop

The commands above are the interactive lifecycle, driven one run at a time by
a human or the hosted chat API. `scripts/selfmod_forever.py` drives that same
`selfmod` lifecycle unattended, for a bounded number of hours, and is what a
nightly/background job should call.

It is deliberately more conservative than the interactive lifecycle: every
pass proposes one change, isolates it in a **Git worktree**, runs Ruff (if
installed) and the **whole** test suite there, and on a green result commits
the result to its own `selfmod/<run-id>` branch instead of deploying it. The
main working tree is never written and no commit lands on the branch you have
checked out, so a live session keeps its checkout and its uncommitted work
exactly as it was. Review or discard a result like any other branch:

```bash
git log --oneline --all --grep='^selfmod:'
git log -p selfmod/<run-id>
git branch -D selfmod/<run-id>   # discard a candidate you don't want
```

It refuses to start a pass at all when:

- the repository working tree is dirty (a run started here could never be
  committed, so starting one would only mutate source with nothing to
  review — the loop reports `working tree dirty` and stops);
- `selfmod` is disabled, or its mode is `observe` (proposals only, no
  candidate);
- the model proposes nothing new — objectives that repeat a recent run
  (by fuzzy match, not exact text) or that only touch docstrings, comments,
  or formatting are rejected before a candidate workspace is even created.

Each candidate is also judged on its diff, not its stated objective: a
comment-only change, a rewritten `return`/`raise`, an added `print()`, or a
newly strict lookup that used to have a default are all rejected regardless
of what tests say, because tests alone did not catch these in practice. The
eligible file list is `nightly_selfmod.CANDIDATE_FILES` — a fixed set of
smaller, well-tested modules; `server.py` and every protected-policy path
(`selfmod.protected_paths()`) are permanently excluded from unattended edits.

### Running it

Start it detached so it survives the launching session (a child of an agent
session gets reaped long before an hours-long loop finishes):

```powershell
powershell -NoProfile -File scripts\start-selfmod.ps1 -Hours 4
powershell -NoProfile -File scripts\start-selfmod.ps1 -Hours 4 -MaxBarren 0
powershell -NoProfile -File scripts\start-selfmod.ps1 -Status
powershell -NoProfile -File scripts\start-selfmod.ps1 -Stop
```

`start-selfmod.ps1` refuses to start a second instance and refuses to start on
a dirty tree, checked before launch rather than left for the child process to
discover. Flags and defaults:

| Flag | Default | Meaning |
|---|---|---|
| `-Hours` | `4.0` | Wall-clock budget; checked before each pass, never mid-pass. |
| `-Model` | `qwen2.5-coder:14b` | Installed Ollama tag passed as an explicit catalog selector, not a tier override. |
| `-MaxBarren` | `6` | Stop early after this many consecutive non-committing passes (`0` disables the limit). |
| `-NumCtx` | `16384` | Local generation context window for selfmod prompts. |
| `-Python` | *(auto)* | Interpreter to launch with; falls back to `$env:SONDER_PYTHON`, then `venv\Scripts\python.exe`, then `python` on `PATH`. |

Output goes to `%LOCALAPPDATA%\sonder\selfmod-continuous.log` (and `.err`
for stderr) rather than a pipe, since a pipe dies with its reader once the
launching session exits. Calling `scripts/selfmod_forever.py` directly
(for a non-Windows host, or from an already-detached process) takes the same
budget as `--hours`/`--model`/`--max-barren`/`--num-ctx`/`--test-timeout`
long-form flags.

At the start of every run the loop reclaims orphaned runs: a pass that was
killed mid-flight (the common failure mode before this existed) leaves an
`editing`/`testing` run with no owner and an ~100 MB worktree that no other
cleanup path reclaims. Anything in that state with no owner predates the
current loop and is cancelled and discarded, since only one loop runs at a
time.

## Emergency recovery

If Sonder Runtime cannot import or start, use the standalone stdlib-only
script. It does not import `server`, `selfmod`, or any application module:

```bash
python /absolute/path/to/selfmod_recover.py \
  /absolute/path/to/SONDER_HOME/selfmod/backups/<run-id>/manifest.json
```

On Windows:

```bat
py C:\absolute\path\selfmod_recover.py %LOCALAPPDATA%\sonder\selfmod\backups\<run-id>\manifest.json
```

The command verifies the manifest checksum and every backup hash, atomically
restores existing files, removes only files recorded as newly created, verifies
the restored SHA-256 values, and aborts on any corruption.
