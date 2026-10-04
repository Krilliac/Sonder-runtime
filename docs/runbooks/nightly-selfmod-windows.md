# Windows nightly selfmod deployment

The scheduled entrypoint is [run-nightly.ps1](../../scripts/run-nightly.ps1).
It waits for Python, writes separate dated stdout/stderr logs and returns the
Python exit code to Task Scheduler. The learning cycle also prepares selfmod
candidates in separate Git worktrees. Passing candidates become local
`selfmod/<run-id>` branches for human review. They are never pushed, merged or
installed into the stable checkout by this scheduled path.

This guide supplies the Windows deployment documentation requested by
[issue #517](https://github.com/Krilliac/Sonder-runtime/issues/517). It does not
qualify SELFMOD-002/003: a current Windows isolated baseline, scheduler
qualification and an independent promotion evaluator remain incomplete.

## Account and credentials

Use a dedicated standard Windows account with its own initialized user
profile, runtime configuration and state directory. Log in once to initialize
the profile and verify Python, Git and the configured model endpoint under
that account. The account needs read/execute access to its interpreter and
tools and write access to the checkout's Git metadata, selfmod worktree and
backup locations, runtime state, scratch and logs. Keep this installation
separate from an interactive checkout with uncommitted work.

The evaluator and scheduler run at that account's normal integrity level;
candidate tests use a restricted low-integrity token and a Job Object.
Installing as `SYSTEM`, an administrator, or with **Run with highest
privileges** adds authority this boundary has not been qualified to contain.
Use **Run with highest privileges: off**. Do not grant elevation to work
around an isolation failure.

Choose the logon type according to the resources the task actually uses:

| Task setting | Credential requirement and limit |
| --- | --- |
| Run only when user is logged on | Uses the interactive logon token; unsuitable for overnight operation after logout. |
| Run whether user is logged on or not | Enter the dedicated account's password in the Task Scheduler credential dialog. Windows stores the task credential; re-enter it after a password change. The account must have **Log on as a batch job**, and policy must not deny that right. |
| Do not store password | Uses S4U and has no network credentials or access to encrypted files. Use it only after validating the complete task with local, unencrypted resources; mapped shares, integrated remote authentication and encrypted credential material may fail. |

Do not put account passwords, API tokens or SSH credentials in task arguments,
task XML, launcher scripts, Git, or logs. Enter the account password through
the scheduler dialog. Provision any required provider credentials separately
for this account, using the runtime's private credential storage. Interactive
user credentials, mapped drives and a PowerShell profile are not inherited
by a noninteractive task. A plain unauthenticated TCP model endpoint does not
require Windows network credentials; test each authenticated remote route
with the selected task logon type.

Low integrity does not provide a general read, network or desktop/broker
boundary. The supervisor protects Sonder's recognized state secrets with
no-read-up labels and refuses when protection fails, but this does not protect
every credential available to the account. Give the account only the data and
credentials needed for this job. A confidential independent oracle is not
implemented on Windows.

## Fixed installation paths

Use a short local checkout, for example `C:\Sonder`, with
`C:\Sonder\venv\Scripts\python.exe`. Install the repository's runtime and
development requirements in that venv, including `pywin32` and
`pytest-xdist`. Install optional dependencies required by the test partitions
being qualified. Confirm Git is available to this account.

The current wrapper forwards its Python argument list through `Start-Process`;
use a checkout path without spaces. The low supervisor separately requires
candidate working directories of at most 258 characters and a scratch work
directory of at most 48 characters. Windows long-path support does not remove
these explicit supervisor checks.

Create an operator-owned launcher, for example
`C:\Sonder\ops\launch-nightly.ps1`, with these account-specific paths:

```powershell
[CmdletBinding()]
param([switch] $Preflight)
$ErrorActionPreference = 'Stop'
$env:SONDER_HOME = 'C:\SonderState'
$env:SONDER_CONFIG = 'C:\SonderState\sonder.toml'
$env:SONDER_SELFMOD_SCRATCH_ROOT = 'C:\SonderScratch'
& 'C:\Sonder\scripts\run-nightly.ps1' `
  -Python 'C:\Sonder\venv\Scripts\python.exe' `
  -LogDirectory 'C:\SonderState\nightly-logs' `
  -Preflight:$Preflight
exit $LASTEXITCODE
```

Set the runtime configuration's state home to the same location. Create state,
logs and scratch as this account, with access restricted to the account and
trusted administrators. Keep the launcher, configuration, evaluator code,
stable checkout, ledger and backups at normal integrity. The supervisor
creates and labels disposable low-integrity scratch itself; do not lower the
integrity label of the installation, evaluator truth or state to make a test
pass. Configuration and state changes should be applied while the task is
disabled.

## Register and check the task

1. As the dedicated account, run the launcher with `-Preflight`. Confirm a
   dated `wrapper-*.log` contains `nightly preflight ok` and `exit=0`. Preflight
   validates checkout/provider configuration only; it does not run a candidate,
   test isolation, contact a model or establish independent grading.
2. In Task Scheduler, choose **Create Task**, name it `SonderNightly`, select
   the dedicated account and the logon setting above, and leave highest
   privileges off. Add a daily trigger outside the interactive workload.
3. Use `C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe` as the
   action, arguments `-NoProfile -NonInteractive -File
   "C:\Sonder\ops\launch-nightly.ps1"`, and **Start in** `C:\Sonder`.
   Use the machine's approved script execution policy; sign/authorize the
   launcher where that policy requires it.
4. Set **If the task is already running: Do not start a new instance**. The
   Python cycle also holds `nightly.lock`. Configure a finite task execution
   limit long enough for the bounded gates and requested rounds; forced
   termination is recovery evidence to investigate, not a passing evaluation.
5. Run the task manually through Task Scheduler, then check the same task
   after logout and after a reboot. Use Task History and **Last Run Result**
   together with stdout, stderr and the selfmod ledger. Exit zero alone does
   not mean candidate evaluation passed: disabled selfmod, unavailable
   isolation or a rejected candidate can be ordinary logged outcomes.

The wrapper waits for Python; `0x41301` means the task is still running, not
complete. Check both `wrapper-*.log` and its `.stderr` file, plus the cycle's
`YYYY-MM-DD.log`. Inspect the selfmod run's gate attestations, exact tested
digests, baseline and decision before accepting an improvement.

If a task is killed, disable its trigger while inspecting the lock and the
last journal stage. Do not delete a lock owned by a live process. The cycle
reclaims a lock only after its age threshold and an owner-dead check; an
uncertain selfmod effect needs trusted reconciliation. Retain the sealed
backup and run ledger. Resume scheduling only after confirming the stable
checkout is healthy and the previous process tree has ended.

## Resource and promotion limits

The ordinary regression partition allows 4 GiB per process, an aggregate
budget of `min(24 GiB, max(6 GiB, 4 GiB * workers + 2 GiB))`, and a bounded
process count. The heavy partition allows 6 GiB per process and 8 GiB per job.
The default worker count considers CPU count and Windows available commit
charge, reserving 25 percent of commit headroom; an explicit
`SONDER_SELFMOD_REGRESSION_WORKERS` is capped at 12. These are ceilings, not
RAM reservations or a guarantee that the host can finish its tests.

The effective test timeout is capped by the run's `max_test_seconds` budget
(900 seconds by default), even though the nightly driver requests 1800
seconds. The historical two-worker Windows baseline took 1635 seconds for
the ordinary partition and did not pass. The heavy-memory budget has since
been implemented; a current measured baseline under those budgets is still
required. See the [historical qualification receipt](../architecture/evidence/SELFMOD-002-LOW-INTEGRITY-BASELINE-2026-09-23.md).

Git for Windows' MSYS2 runtime cannot start at low integrity. Tests marked
`requires_medium_integrity` are excluded from candidate execution and recorded
as `regression_medium: NOT EVALUATED`. They must never be run as candidate
code at the evaluator's normal integrity to manufacture a passing gate.
This unevaluated partition blocks scheduled promotion on Linux as well as
Windows. Changing the mode to `auto-low-risk` does not remove it.

Linux can additionally grade with evaluator-owned private held cases through
the uid-separated supervisor. The operator must provision an independent
case corpus for each target and preserve its confidentiality; repository
assertions do not supply that corpus. Windows low integrity supplies no such
independent oracle. Unsupported hosts refuse before a candidate run starts:
macOS, Linux without a configured dedicated uid/root supervisor and required
kernel facilities, and Windows without usable `pywin32`/token/Job support.
See [Linux/operator isolation requirements](../architecture/REMAINING-SELFMOD-517-LINUX-ISOLATION.md).

Human review and deployment are separate from this task. Deployment requires
the recorded tested bytes and a verified backup and rechecks the baseline;
it uses a process-safe deployment lock and admits exactly one changed checkout
file for atomic replacement. Empty and multi-file diffs refuse before any live
write, including under maintenance or human approval. Coupled changes require
the managed staged release workflow; retain their candidate and review evidence.
The complete scoped backup is verified and sealed before its `backed_up`
phase is published; candidate execution requires that phase. This establishes
the rollback point required by the nightly isolation contract. Interrupted
deployment still requires reconciliation. Scheduler deployment remains
disabled while required evaluation partitions have not been evaluated.
