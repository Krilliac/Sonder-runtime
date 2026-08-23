---
name: sonder-selfmod-lifecycle
description: >-
  Runbook for Sonder Runtime's self-modification (selfmod) subsystem: phases, gates,
  protected paths, deploy proof obligations, and recovery. TRIGGER when the user says
  "selfmod", "self-modification", "candidate rejected", "selfmod deploy", "rollback the
  deploy", "protected path", "nightly selfmod", or "selfmod resume". DO NOT TRIGGER for
  general commit/PR/approval gating of ordinary code changes (use sonder-change-control),
  for build/test environment setup (sonder-build-and-env), or for reading historical
  incident lore on its own (sonder-failure-archaeology).
---

# Sonder selfmod lifecycle

Selfmod is the machinery by which Sonder Runtime edits its own source: a
host-controlled state machine in `selfmod.py` that builds an isolated candidate,
grades it with deterministic checks, and only then atomically replaces live
files — with an immutable backup, a rehearsed rollback, and an append-only audit
ledger. The candidate model's output is one untrusted input. It can edit only an
isolated workspace; it cannot approve itself, decide that tests passed, deploy,
edit backups, or start another run (`SELFMOD.md:3-7`, recursion guard at
`selfmod.py:1883-1885` via the `SONDER_SELFMOD_ACTIVE` env var).

## When NOT to use this skill

| You are doing... | Use instead |
|---|---|
| Ordinary code review, commit discipline, approval flow for human edits | `sonder-change-control` |
| Setting up Python/venv, running the test suite generally | `sonder-build-and-env` |
| Digging into why a past gate was added (incident history) | `sonder-failure-archaeology` |
| Operating the server/REPL day to day | `sonder-run-and-operate` |

Use THIS skill whenever a run id, a `/selfmod` command, a `selfmod.db` row, a
backup manifest, a rejected candidate, or `scripts/nightly_selfmod.py` is
involved.

## Vocabulary

| Term | Meaning |
|---|---|
| Run | One lifecycle attempt, row in `selfmod_runs`, id is a UUID hex string |
| Candidate | The edited copy of the source, living only in the run's workspace |
| Workspace | Isolated checkout at `<SONDER_HOME>/selfmod/workspaces/<run-id>/` (detached git worktree on branch `selfmod/<run-id>`, or a full snapshot for non-git installs) |
| Backup bundle | `<SONDER_HOME>/selfmod/backups/<run-id>/` — `manifest.json`, `manifest.sha256`, `files/...`; hash-verified before edit, deploy, rehearsal, restore |
| Gate | A deterministic check recorded in `selfmod_tests` with a `kind`; `review()` requires specific kinds to have passed |
| Receipt | A SHA-256 digest a probe must print, computed by the recording process and never given to the probe — exit 0 alone never satisfies a receipted gate |
| Protected path | A file matching `selfmod.protected_paths()`; automatic edits refuse it, maintenance runs need explicit user approval |
| Lease | Cross-process ownership with `LEASE_SECONDS = 180` (`selfmod.py:70`); one deployment lock row (`selfmod_deployment_lock`, single row, `selfmod.py:112-116`) |

## Phase machine

Phases (`selfmod.py:33-37`):

```text
observed -> proposed -> backed_up -> editing -> testing -> reviewing
         -> approved -> deployed

editing/testing/reviewing -> rejected  -> restored
deployed                  -> rollback_requested -> restored
any active phase          -> cancelled  (not from deployed; selfmod.py:1591-1595)
stale-owner editing/testing/reviewing -> interrupted (selfmod.py:1598-1609)
```

Modes (`selfmod.py:32`): `observe` (inspect only), `propose` (default — build and
test a candidate, deployment needs explicit approval), `auto-low-risk`
(deterministic host checks may approve only low-risk, non-protected,
non-approval-required work). Kill switch: `/selfmod disable`, re-enable with
`/selfmod enable`.

Every transition is funneled through the host; nothing the candidate writes can
move a phase. `interrupted` runs never resume on their own — only
`/selfmod resume <run-id>` does (`selfmod.py:1654-1659`), and it returns the run
to `editing` (if a workspace exists) or `backed_up`.

## State layout (all OUTSIDE the repo)

```text
<SONDER_HOME>/selfmod/selfmod.db                       # SQLite, WAL mode
<SONDER_HOME>/selfmod/backups/<run-id>/manifest.json
<SONDER_HOME>/selfmod/backups/<run-id>/manifest.sha256
<SONDER_HOME>/selfmod/backups/<run-id>/files/...
<SONDER_HOME>/selfmod/workspaces/<run-id>/...
```

`SONDER_HOME` defaults to `%LOCALAPPDATA%\sonder` on Windows
(`sonder_runtime/platform/paths.py:156-158`). `SONDER_SELFMOD_HOME` and
`SONDER_SELFMOD_DB` relocate state (`selfmod.py:120-126`), but the state
directory must stay outside the editable repository — the backup is what
survives a bad deploy.

Tables (`selfmod.py:72-117`): `selfmod_runs` (full run row incl. phase, budgets,
owner lease, `git_status_start`, test inventories), `selfmod_backups`,
`selfmod_tests` (every gate execution with argv, exit code, output),
`selfmod_deployed_files` (post-deploy hash of every changed path),
`selfmod_events` (append-only audit), `selfmod_deployment_lock` (single row).

Budgets (`selfmod.py:57-67`): 8 files changed max, 600 lines changed max, 900 s
per test, 1800 s per run, among others. Oversize diffs are rejected at
`selfmod.py:729`.

## Command surface

From the REPL / chat (`server.py:2502-2622`; help text at `server.py:2613-2619`):

```text
/selfmod status | opportunities | history
/selfmod inspect <run-id>
/selfmod plan <objective> --files module.py,tests/test_module.py
/selfmod plan <objective> --maintenance --files protected.py,tests/test_security.py
/selfmod run  <objective> --files module.py,tests/test_module.py --tests python -m pytest -q tests/test_module.py
/selfmod diff <run-id>          /selfmod tests <run-id>
/selfmod approve <run-id>       /selfmod reject <run-id> [reason]
/selfmod deploy <run-id>        /selfmod rollback <run-id>
/selfmod backups                /selfmod verify-backup <run-id>
/selfmod mode observe|propose|auto-low-risk
/selfmod resume <run-id>        /selfmod cancel <run-id>
/selfmod disable | enable
/selfmod retention <days> <max-gb>
/selfmod prune-backups
```

`deploy` and `rollback` are the only two actions that write the live tree, and
they refuse to run unattended: `_SELFMOD_SOURCE_WRITING_ACTIONS`
(`server.py:2499-2520`) requires either a real console operator approval or an
explicit `/permissions` allow rule for `selfmod`. "Nobody was available to ask"
resolves to NO for these two — see `tests/test_selfmod_deploy_gate.py` for the
rationale. The hosted chat API mirrors the same slash lifecycle with
developer/admin auth; `/v1/sonder/status`
(`sonder_runtime/interfaces/http/serve.py:4081`) exposes mode, phase summaries,
active runs, backup root, and rollback-point count.

## Runbook: a human-driven run

```text
1. /selfmod plan Fix X in module.py --files module.py,tests/test_module.py
2. /selfmod run  Fix X in module.py --files module.py,tests/test_module.py \
       --tests python -m pytest -q tests/test_module.py
   # For protected files: add --maintenance and TWO test commands
   # separated by ' ;; ' — reproducer, then the security suite
   # (server.py:2234-2236 builds the "security" gate from the second).
3. /selfmod diff <run-id>     # read the complete host-inventoried diff
4. /selfmod tests <run-id>    # read every recorded gate result as JSON
5. /selfmod approve <run-id>  # only valid from phase reviewing
6. /selfmod deploy <run-id>   # console-approved; runs rollback probe + health
7. If anything looks wrong afterwards: /selfmod rollback <run-id>
```

What `run` does behind the scenes (`server.py:2301-2368`): creates backup,
prepares the workspace, claims a lease with a 30 s heartbeat thread, records
`reproducer_before` against the UNTOUCHED live source (must FAIL there — exit
nonzero — to prove the defect exists; `selfmod.py:774-780`), lets the guarded
workbench agent edit only the workspace with file tools, then records the gate
battery and calls `review()`. The agent is told, and prevented, from approving,
deploying, or touching the live repo.

## The review() gates — why candidates get rejected

`review()` (`selfmod.py:983-1044`) runs in phase `testing` and either advances
to `reviewing` or moves to `rejected` and restores. Default required passing
kinds: `reproducer_before`, `syntax`, `targeted`, `regression`, `smoke`; plus
`security` when `maintenance_authorized` (`selfmod.py:990-992`). Callers may
narrow with `require_kinds` (the nightly lane passes `{"syntax","regression"}`,
`scripts/nightly_selfmod.py:750`).

Exact rejection reasons written to `last_error` (match these when triaging
"candidate rejected"):

| `last_error` contains | Cause | Fix |
|---|---|---|
| `missing passing checks: ...` | A required kind was never recorded as passing | Record the named gate; for smoke use `selfmod.record_smoke`, never a hand-rolled command |
| `one or more recorded checks failed` | Any recorded gate failed, even a non-required one | `/selfmod tests <id>`, read the failing row's output |
| `original failure was not demonstrated before editing` | `reproducer_before` did not FAIL (nonzero exit) on the live source | The bug must be reproducible pre-edit; for additive work, drive via a lane that waives it with `require_kinds` |
| `candidate produced no scoped diff` | Edit changed nothing inside declared files | Re-scope or re-edit |
| `protected file modified` | Diff touches a protected path without `--maintenance` | See protected policy below |
| `pre-existing required tests were modified: ...` | Candidate edited a test file that existed before (weakened surface, `selfmod.py:1015-1027`) | Never edit existing tests in a selfmod run; add new ones |
| `test inventory was weakened` | Before-inventory is not a subset of after (`selfmod.py:1028-1031`) | Restore removed/renamed tests |
| `rollback rehearsal failed` | Backup could not be dry-restored to a temp dir with matching hashes (`selfmod.py:967-980`) | `/selfmod verify-backup <id>`; a corrupt bundle fails closed |

On pass, auto-approval happens ONLY when mode is `auto-low-risk` AND risk is
`low` AND `approval_required` is false (`selfmod.py:1042-1043`). `approve()`
additionally refuses any `host:*` approver for high/critical risk
(`selfmod.py:1051-1052`).

## The smoke gate and the receipt principle

`record_smoke` (`selfmod.py:915-958`) splits the run's declared `.py` files into
must-import (still present in the candidate) and must-be-gone (declared but
deleted) via `smoke_plan` (`selfmod.py:886-900`). A stdlib child process rooted
at the workspace (`_SMOKE_PROBE`, `selfmod.py:804-870`) imports each surviving
module, verifies each import resolved INSIDE the workspace, verifies each
deleted module is genuinely unresolvable, and prints
`SELFMOD-SMOKE-RECEIPT <sha256> modules=N gone=M`.

The expected receipt is computed by the recording process from the bytes on disk
(`selfmod.py:903-912`) and never passed to the probe; `_record_command` refuses
a passing exit code whose output lacks the receipt (`selfmod.py:753-764`). An
empty declared-Python set is a refusal, not a pass (`selfmod.py:939-951`), and
the same rule applies to the syntax gate built in `server.py:2201-2223`.

Why this shape: the smoke gate was once literally
`python -c "import pathlib; assert pathlib.Path('.').is_dir(); ..."` — a
required gate that could not fail (fixed in commit `7bdfa19f`). The full story
is in `tests/test_selfmod_smoke_gate.py:1-24` and in
`sonder-failure-archaeology`. The operating rule it left behind: exit 0 is not
proof of work; a gate must demand evidence only real work can produce.

## Protected policy

`selfmod.protected_paths()` returns the canonical policy
(`selfmod.py:207-208`); `selfmod.is_protected_path(path)` answers for one path
(`selfmod.py:211-213`). A path is protected when it starts with any
`SENSITIVE_PREFIXES` entry or contains any `SENSITIVE_PARTS` substring,
case-insensitively (`selfmod.py:199-204`).

Prefixes (`selfmod.py:40-52`) include: `permission_rules.py`, `admin_auth.py`,
`file_ops.py`, `safe_update.py`, `selfmod.py`, `selfmod_recover.py`,
`server.py`, `reloadable_mcp.py`, `tool_contract.py`, `model_transport.py`,
context overflow/compaction modules, `process_liveness.py`,
`ollama_endpoint.py`, `autopilot_controller.py`, `autopilot_store.py`,
`sonder_paths.py`, `sonder_serve.py`, `deploy_`, `sonder-runtime`,
`tests/test_permission`, `tests/test_admin`, `tests/test_control_plane`,
`tests/test_read_only_agent_policy`, `tests/test_selfmod`.

Substrings (`selfmod.py:53-56`): `.env`, `credential`, `secret`, `token`,
`account`, `migration`, `permissions.json`, `selfmod_policy`, `selfmod.db`,
`audit`.

Rules:

- Automatic edits can NEVER touch a protected path — `review()` rejects the run.
- Protected maintenance requires BOTH an explicitly authorized maintenance run
  (`--maintenance`, which sets `maintenance_authorized` and adds the required
  `security` gate) AND explicit user approval. `auto-low-risk` can never approve
  it (`SELFMOD.md:67-74`).
- The nightly lane filters protected paths out of its candidate list before
  proposing anything (`scripts/nightly_selfmod.py:183-188`).

## Deploy: the exact sequence and its proof obligations

`deploy(run_id)` (`selfmod.py:1424-1524`) runs entirely under the single-row
cross-process deployment lock (`selfmod.py:1102`, lease renewed at each long
step):

1. Phase must be `approved`; anything else raises.
2. Backup bundle is re-verified hash-by-hash.
3. Source-conflict check: starting commit and dirty-tree fingerprint of the
   declared files must match what the run recorded (`selfmod.py:1121-1126`).
4. Scope check: the candidate's changed files must still be a subset of the
   approved file list (`selfmod.py:1435-1436`).
5. Each file is replaced via same-directory temp file + `fsync` + `os.replace`;
   the matching `__pycache__` bytecode is removed (`selfmod.py:1439-1447`,
   `1225-1233`). Deletions happen only for declared files removed in the
   candidate.
6. Git commit `selfmod: <objective>` is recorded ONLY when the run started on a
   clean tree (`selfmod.py:1450-1470`). A checkout that was already dirty is
   deployed WITHOUT a commit so unrelated staged/unstaged user work is
   untouched.
7. Post-deploy hashes of every changed path are recorded in
   `selfmod_deployed_files` (`selfmod.py:1471-1473`) — this is what later lets
   rollback detect user edits and refuse to clobber them.
8. **Rollback readiness, first and unconditionally** (`selfmod.py:1475-1504`):
   a child process running the JUST-DEPLOYED bytes (`rollback_probe_command`
   puts `.` first on `sys.path`, `selfmod.py:1377-1392`) dry-runs BOTH rollback
   routes in temp directories — the in-tree manifest restore that
   `/selfmod rollback` uses, and the out-of-tree `selfmod_recover.restore`
   driven through its own bundle so its checksum gate and path confinement
   execute too (`verify_rollback_ready`, `selfmod.py:1288-1374`). Both must
   produce identical digests matching an expected receipt computed by the
   DEPLOYING process from the manifest and never given to the child
   (`selfmod.py:1249-1265`, `1405-1421`). This runs even when no health command
   is supplied, because the nightly lane calls `deploy(run_id)` bare and a
   `--maintenance` run is allowed to rewrite the recovery path itself
   (comment at `selfmod.py:1475-1489`). Failure → automatic restore of exact
   backup hashes.
9. Health command, if supplied, runs next. The console's default is
   `python -c "import server; print(server.status())"` (`server.py:2595`). It
   proves the new bytes import; it is NOT evidence that rollback works, and it
   exits 0 even when `status()` returns an Ollama error string
   (`server.py:2583-2587`) — do not grow claims for it. Nonzero exit →
   automatic restore.
10. Modules on the conservative live-reload allowlist (`LIVE_RELOAD_MODULES`,
    `server.py:908`) are reloaded in-process; a reload error triggers
    `selfmod.rollback` too (`server.py:2600-2609`).
11. Any exception while still in `approved` also restores
    (`selfmod.py:1519-1524`). Nothing in deploy pushes, fetches, rebases,
    resets, cleans, installs dependencies, or rewrites history.

## Rollback and recovery

**Manual rollback** (`/selfmod rollback <id>`, `selfmod.py:1576-1588`): valid
only from `deployed`, takes the deployment lock, and first compares live files
against the recorded post-deploy hashes. If the user edited a deployed file
afterwards, rollback REFUSES and preserves the current bytes, reporting
`rollback conflict: deployed files changed after deployment` — resolve
explicitly, never force. On restore of a clean-start run whose deploy made a
commit, a `selfmod rollback: <objective>` commit records the reversal
(`selfmod.py:1560-1572`).

**Crash recovery**: `reconcile_stale_deployment` (`selfmod.py:1612-1651`)
atomically transfers a stale deployment lease to a recovery owner and restores
exact backups; a live local owner cannot lose its lock just because the lease
timestamp expired. `reconcile_interrupted` (`selfmod.py:1598-1609`) marks
editing/testing/reviewing runs with dead owners `interrupted`; resume them only
with `/selfmod resume <run-id>`.

**Emergency (Sonder cannot import or start)** — stdlib-only, imports no Sonder
module BY DESIGN; adding an application import to `selfmod_recover.py` breaks
the out-of-tree route that deploy cross-checks:

```powershell
py C:\path\to\selfmod_recover.py $env:LOCALAPPDATA\sonder\selfmod\backups\<run-id>\manifest.json
```

`selfmod_recover.restore` (`selfmod_recover.py:119-134`) verifies the manifest
checksum against `manifest.sha256`, validates the WHOLE bundle before touching
the repo (`_validated_manifest`, `selfmod_recover.py:52-116`): bounds of 256
files, 4096-char paths, 512 MiB per file (`selfmod_recover.py:12-14`), path
confinement to the repo root and the bundle, checksum of every backup, then
atomically restores existing files, removes only files recorded
`existed_before=false`, and re-verifies restored SHA-256s. Any corruption
aborts before the first write.

**Retention**: `/selfmod retention <days> <max-gb>` and
`/selfmod prune-backups`; pruning never deletes the newest valid rollback
bundle (`SELFMOD.md:64-65`). Finished runs' workspaces are pruned separately
(`prune_workspaces`, `selfmod.py:1690-1717` — each is ~100 MB).

## The nightly lane (`scripts/nightly_selfmod.py`)

One full lifecycle per night, driving `selfmod` end to end. Invariants
(docstring, `scripts/nightly_selfmod.py:19-35`):

- Never bypasses the configured mode. Under `propose` a candidate stops at
  `reviewing` and waits for `/selfmod approve`; only the operator-set
  `auto-low-risk` allows unattended approve+deploy
  (`scripts/nightly_selfmod.py:784-792`).
- Refuses to start on a dirty tree (`scripts/nightly_selfmod.py:615-623`) — a
  dirty-start run could only produce an uncommitted edit.
- Never touches a protected path (candidate files are filtered through
  `selfmod.is_protected_path`, lines 183-188), never merges, never pushes.
- The regression command is the WHOLE suite, `python -m pytest -q`, run inside
  the candidate workspace (lines 717-730) — a targeted subset cannot show what
  a change broke elsewhere.
- Ruff is optional: `py_compile` is the required syntax gate; a missing Ruff
  install is logged, not failed (`_ruff_command`, lines 136-164; commit
  `44b01b1e`).
- `_objective_is_actionable` (lines 175-180) rejects comment/docstring-only
  proposals before any workspace is spent.
- Model replies are spliced as ONE AST-validated function back into the module
  (`_splice_function`, lines 216-258); whole-file rewrites converge on deletion
  (measured 49-50% of the file returned by a 14B model, lines 672-682).
- The spliced file must keep at least 75% of the original length — the same
  `SHRINK_FLOOR = 0.75` deletion guard as `code_improve.py:29`
  (`scripts/nightly_selfmod.py:689-696`).
- `review()` is called with `require_kinds={"syntax", "regression"}` and its
  VERDICT is honoured by phase, not assumed (lines 739-760).
- With `branch=True` (default) a passing candidate is committed to its own
  `selfmod/<run-id>` branch inside the worktree — the main tree is never
  written; `deploy` remains the only installing path (lines 762-782).

`code_improve.py` is the reusable half: `improve_function(source, name,
ask_fn, ...)` takes an injected `ask_fn(prompt, tier) -> str` seam, never writes
a file itself, and returns a result dict for the caller to apply or reject
(`code_improve.py:13-15`, `244-248`).

Run it manually (venv Python; see `sonder-build-and-env` for setup):

```powershell
python scripts\nightly_selfmod.py
```

## Working ON selfmod itself

- `selfmod.py`, `selfmod_recover.py`, and `tests/test_selfmod*` are all
  protected prefixes — changing them through selfmod is `--maintenance` work
  needing a `security` gate and explicit user approval; changing them by hand
  goes through normal review (`sonder-change-control`).
- The acceptance surface is the test battery: `tests/test_selfmod.py`,
  `test_selfmod_smoke_gate.py`, `test_selfmod_deploy_gate.py`,
  `test_selfmod_deploy_health.py`, `test_selfmod_governance_reproducer.py`,
  `test_selfmod_verification_lifecycle.py`, `test_selfmod_commands.py`,
  `test_selfmod_legacy_integration.py`. Tests isolate state with
  `SONDER_SELFMOD_HOME`/`SONDER_SELFMOD_DB` pointed at a temp dir
  (`tests/test_selfmod_smoke_gate.py:44-50`) — do the same in any new test.
- **Every new gate must be mutation-proved**: plant a failing candidate (broken
  import, forged receipt, deleted module, no-op probe) and watch the gate
  refuse. A gate proven only by passing candidates is the `assert
  Path('.').is_dir()` defect rebuilt — a required check that cannot fail
  manufactures the appearance of review (`selfmod.py:915-931`).
- Receipts must be computed by the adjudicating process and never handed to the
  probe (`selfmod.py:800-803`, `1249-1256`). Keep that property when extending
  smoke or the rollback probe.
- `selfmod.py` is deliberately lightweight — stdlib plus only `sonder_paths`,
  `sonder_logging`, and the process-liveness adapter (`selfmod.py:27-29`) — so
  recovery stays available when the rest of Sonder cannot import
  (`selfmod.py:1-7`); `selfmod_recover.py` imports NO Sonder module at all. Do
  not add imports to either.

## Invariants (the never list)

- Candidate output never moves a phase, approves, deploys, or starts a run.
- Selfmod never pushes, fetches, rebases, resets, cleans, installs
  dependencies, or rewrites git history (`SELFMOD.md:168-171`).
- Unrelated dirty user work is never stashed, committed, or reset; dirty-start
  deploys skip the commit entirely.
- Backups live outside the repo; corruption fails closed.
- An empty target set is a refusal, not a pass — for syntax and for smoke.
- Exit 0 without the receipt is a gate FAILURE, recorded as such.
- `deploy`/`rollback` never run with nobody present to approve them.
- The rollback probe runs unconditionally on every deploy, before any health
  command, and its scope is honest: it catches a rollback that is broken, not a
  deployed tree that deliberately forges its own receipt
  (`selfmod.py:1308-1310`).

## Provenance and maintenance

Verified against commit 99162cf9 (2026-08-22). Re-verify before trusting
line-number citations after selfmod changes:

- Phases/modes/protected lists: `python -c "import selfmod; print(selfmod.MODES, selfmod.PHASES); import json; print(json.dumps(selfmod.protected_paths(), indent=1))"` (from repo root).
- Review gate kinds: `rg -n "required = set\(require_kinds" selfmod.py`
- Smoke receipt refusal: `rg -n "SELFMOD-SMOKE-RECEIPT|GATE REFUSED" selfmod.py`
- Deploy sequence and unconditional rollback probe: `rg -n "def deploy|_verify_deployed_rollback|verify_rollback_ready" selfmod.py`
- Console gate on deploy/rollback: `rg -n "_SELFMOD_SOURCE_WRITING_ACTIONS" server.py`
- Recovery bounds: `rg -n "MAX_RECOVERY" selfmod_recover.py`
- Nightly invariants: `rg -n "require_kinds|is_protected_path|pytest" scripts/nightly_selfmod.py`
- Deletion guard: `rg -n "SHRINK_FLOOR" code_improve.py scripts/nightly_selfmod.py`
- Command list: `rg -n "unknown selfmod action|selfmod: status" server.py` and compare with `SELFMOD.md` "Commands".
- History anchors: `git log --oneline 7bdfa19f 0192d056 44b01b1e -3 -- selfmod.py scripts/nightly_selfmod.py`
- Acceptance battery: `python -m pytest -q tests/test_selfmod_smoke_gate.py tests/test_selfmod_deploy_gate.py tests/test_selfmod_deploy_health.py`
