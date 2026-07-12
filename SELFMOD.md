# Safe self-improvement

Trilobite self-improvement is a host-controlled state machine. Candidate model
output can inspect and edit only an isolated candidate workspace; it cannot
approve itself, decide that tests passed, deploy source, edit backups, or invoke
another self-improvement run.

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

State is stored under the per-user Trilobite directory:

```text
<TRILOBITE_HOME>/selfmod/selfmod.db
<TRILOBITE_HOME>/selfmod/backups/<run-id>/manifest.json
<TRILOBITE_HOME>/selfmod/backups/<run-id>/manifest.sha256
<TRILOBITE_HOME>/selfmod/backups/<run-id>/files/...
<TRILOBITE_HOME>/selfmod/workspaces/<run-id>/...
```

`TRILOBITE_SELFMOD_HOME` and `TRILOBITE_SELFMOD_DB` can relocate state, but the
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
authorization for mutating actions. `/v1/trilobite/status` exposes the current
mode, phase summaries, active runs, backup root, and rollback-point count for
the Flutter System page.

## Deployment and crashes

Before deployment, the host re-verifies backups, the complete candidate diff,
the starting commit, dirty-tree fingerprint, scope, inventory, and approval.
Files are replaced with same-directory temporary files plus `fsync` and
`os.replace`. Deletions happen only for declared files whose candidate version
was removed. A separate Python health subprocess imports Trilobite and requests
status. Failure automatically restores exact backup hashes. Already-loaded
helper modules on Trilobite's conservative live-reload allowlist are then
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

## Emergency recovery

If Trilobite cannot import or start, use the standalone stdlib-only script. It
does not import `server`, `selfmod`, or any application module:

```bash
python /absolute/path/to/selfmod_recover.py \
  /absolute/path/to/TRILOBITE_HOME/selfmod/backups/<run-id>/manifest.json
```

On Windows:

```bat
py C:\absolute\path\selfmod_recover.py %LOCALAPPDATA%\trilobite\selfmod\backups\<run-id>\manifest.json
```

The command verifies the manifest checksum and every backup hash, atomically
restores existing files, removes only files recorded as newly created, verifies
the restored SHA-256 values, and aborts on any corruption.
