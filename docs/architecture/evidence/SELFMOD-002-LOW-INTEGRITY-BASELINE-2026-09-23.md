# SELFMOD-002: low-integrity full-baseline regression (2026-09-23)

Status: implemented, **not verified**. The full repository suite does not yet
pass under the Windows low-integrity supervisor (`scripts/selfmod_low_integrity.py`).

## Method

Branch `issue-517-low-integrity` after merging `origin/main` (`1d75cd38`, which
includes #530 bounded capability probes and #540 chat-route prewarm). The
suite was collected at medium integrity (16,583 tests in 1,138 files, excluding
the supervisor-only `tests/test_selfmod_low_integrity.py`) and split
round-robin by file into eight shards. Each shard ran through `run_isolated()`
with its own low token and Job Object:
`python -m pytest -q -rfE --tb=short -p no:cacheprovider --junitxml <low dir>`,
3,600-second bound. A serial single-process run was abandoned after about
four minutes at 1% progress; it could not finish inside the nightly budget.

## Result

Six shards finished and reported 12,259 tests: 12,133 passed, 93 skipped,
33 failed. Two shards stopped early:

- One shard hung for more than 300 seconds in
  `tests/test_managed_runtime_payload.py::test_live_grant_and_payload_changes_refuse_before_any_launch_effect`,
  then crashed with an access violation. This test **also hangs at medium
  integrity** (a solo run exceeded 420 seconds), so this is not a
  low-integrity regression.
- One shard raised `MemoryError` in
  `tests/test_qlora_train.py::test_installed_trainer_stack_builds_real_peft_trainer`.
  The test passes at medium integrity, so the supervisor's 2 GiB per-process
  Job limit is what fails.

About 1,370 tests after those two stopping points did not run.

## Failure classes

1. **MSYS2 runtime is denied at low integrity (24 tests).** Git for Windows'
   `sh.exe` and `bash.exe` abort with
   `NtCreateDirectoryObject(\BaseNamedObjects\msys-2.0S5-...): 0xC0000022`.
   Local `git clone` goes through that runtime, so these tests fail:
   `test_cleanup_merged_branches` (14), `test_safe_update` (3),
   `production/test_history_privacy` (2 clone tests), `test_git_tools` (2),
   `test_workbench_inline_shell` bash/sh (2), and
   `test_runner_languages[bash]`. All pass at medium integrity.
2. **Missing environment (4 tests, fixed here).** The low child got no
   `PATHEXT`, so PowerShell `Get-Command python` failed
   (`test_release_smoke` x2, `test_install_workstation_local`). rustup looked
   in the disposable home (`test_runner_languages[rust]`). The supervisor now
   passes an allowlist of non-secret host variables and existing
   `RUSTUP_HOME`/`CARGO_HOME` directories, which the low token can read but
   not write. A low-integrity rerun of the affected files shows these four
   passing.
3. **Signature check (1 test).**
   `test_legacy_tool_executor::test_verify_artifact_uses_packaged_acquisition_verifier`
   returns `signature: UnknownError` at low integrity and passes at medium.
   The cause has not been found.
4. **Also failing at medium integrity on this host (4 tests):**
   `test_managed_runtime_owner::test_full_manifest_owned_http_and_relaunch`
   (hangs more than 420 seconds), `test_serve_auth` natural-control intents
   (2 failures), and `test_profile_tests::test_two_worker_mode_uses_file_grouped_xdist`
   (this venv has no `pytest-xdist`). Hosted CI on `main` is green, so these
   are host or load conditions, not low-integrity failures.
5. **Uncertain (1 test).**
   `test_app_recovery_http::test_http_original_pending_new_login_and_two_separate_approvals`
   returned phase `unknown` at low integrity. It passes alone at medium
   integrity but takes 192 seconds, so it is sensitive to host load.

## Nightly gate implication

The nightly regression check is capped at `max_test_seconds = 900`. The
unsharded low-integrity suite cannot finish inside that cap without
`pytest-xdist`. Even sharded, the slowest shard took 946 seconds. Before
SELFMOD-002 can be verified, the gate needs working xdist (four or more
workers), an MSYS-free git path or an explicit low-integrity exclusion
policy, and a Job memory limit sized for the torch/PEFT import.

## Follow-up: partitioned gate (2026-09-23, later)

### Git Bash / MSYS2 is an OS boundary

With a low token, MSYS2 aborts in its DLL startup at
`NtCreateDirectoryObject(\BaseNamedObjects\msys-2.0S5-<hash>)`, status
`0xC0000022`. This happens before any file access. MSYS2 derives that
directory name from the installation, so no `HOME`, `TMP` or `TEMP` setting
changes it. A privilege-stripped **medium** token passes the same probe,
with the same low-labelled `HOME`/`TEMP` scratch and the same allowlisted
environment (`sh.exe -c "echo MSYS_OK"`). So the integrity level is the
blocker, not file paths. The global object directory does not let a low
process create a subdirectory.

The only workaround inside the low boundary is for the medium supervisor to
pre-create that directory with a low label. That would let a low candidate
plant named objects in the directory that the user's own medium-integrity
MSYS2 processes (for example `git`) share. I rejected that approach.

**Decision (revised after the security review of `cb786816`).** The 24
affected tests carry `@pytest.mark.requires_medium_integrity` and are **not
run against the candidate at all** in the unattended gate.

An interim design (`cb786816`) ran them against the candidate at medium
integrity in a `regression_medium` gate. That was unsafe and has been
removed. Medium-integrity candidate code could:

- rewrite its own tested file after the gates passed;
- write the selfmod database;
- plant `.git` hooks or refs that the nightly `git commit` then executes;
- modify the live checkout or the scheduled task.

A Job object does not contain processes that it starts through WMI or the
Task Scheduler. Most candidate modules are reachable from those tests
(`tests/conftest.py` imports `server`), so "only candidates that do not touch
imported modules" is not a meaningful restriction.

The nightly stage now records these tests as `regression_medium: NOT
EVALUATED -- requires human review`. `selfmod.review(..., unevaluated=...)`
never auto-approves a run that has unevaluated checks, and the nightly stage
never approves or deploys such a run unattended. In branch mode it still
commits to a local, never-pushed `selfmod/<run-id>` branch for human review.
The supervisor no longer has a medium-integrity mode.

`tests/test_selfmod_low_integrity.py::test_git_bash_cannot_start_at_low_integrity`
pins the OS boundary.

### Job memory

The torch/PEFT trainer test peaks at about 2.2 to 2.4 GiB. It carries
`@pytest.mark.heavy_memory` and runs in the required low-integrity
`regression_heavy` gate (6 GiB per process). Job limits are now bounded
parameters of `run_isolated()`. Every recorded result reports the integrity
level, the limits and the observed peaks.

The first partitioned run showed that one long-lived xdist worker grew to the
2 GiB per-process limit near 99% and died with
`RuntimeError: unable to start watchdog thread`. The low partition now gets
4 GiB per worker.

### Runtime

`pytest-xdist` is already in `requirements-dev.txt`. The low partition uses
xdist inside the Job when it is installed. The worker count is bounded: it
defaults to half the cores (at most 8) and can be overridden with
`SONDER_SELFMOD_REGRESSION_WORKERS`, capped at 12.

The local measurement used **2 workers**, because concurrent CMake builds
limited RAM on the host. That run took about 52 minutes for the low
partition, far over the 900-second cap. At that rate, the 900-second cap
would need about 8 or more workers on an idle host. That figure is a
projection, not a measurement. The shared venv lacks xdist, so the
measurement loaded it from a scratch `--target` directory through
`PYTHONPATH`. The venv itself was not modified.

### Other failures, by traceback

- `test_legacy_tool_executor::test_verify_artifact_uses_packaged_acquisition_verifier`:
  a false green on main. It passes only when `powershell.exe` cannot load
  `Get-AuthenticodeSignature` (inherited pwsh-7 `PSModulePath`), because the
  verifier then fails open with `not checked`. It fails with `UnknownError`
  whenever PowerShell works. This is the same result at low and medium
  integrity under the clean environment. Issue #551.
- `test_managed_runtime_payload::test_live_grant_and_payload_changes_refuse_before_any_launch_effect`
  and `test_managed_runtime_owner::test_full_manifest_owned_http_and_relaunch`:
  `RuntimePayload.inventory()` SHA-256-hashes the whole venv (49,782 files,
  3.55 GB, just under the 50,000-file / 4 GiB bounds). They do not finish in
  420 s at medium integrity either. Issue #550.
- `test_extension_experiments::test_running_experiment_must_stop_before_delete_and_close_is_bounded_cleanup`:
  intermittent under low integrity (1 of 3 single runs failed).
  `EphemeralExperimentManager.close()` used a single
  `rmtree(ignore_errors=True)`. It now uses the same bounded
  handle-release retry as `delete()`, and 8 of 8 single low runs passed.
- `test_master_orchestrator::test_cancel_master_skips_queued_workers_and_discards_running_result`:
  also fails when run alone at medium integrity
  (`cannot schedule new futures after interpreter shutdown`). It depends on
  test order or timing and is not specific to low integrity.

### Full partitioned baseline after these fixes

The full suite ran through the three nightly partitions, each under
`run_isolated()` with the exact marker expressions and Job settings from
`scripts/nightly_selfmod.py`. The low partition used 2 xdist workers. Every
selected test ran; no worker or shard was lost.

| Partition | Integrity | Selected | Passed | Failed | Skipped | Wall time | Peak memory (process / job) |
|---|---|---|---|---|---|---|---|
| `regression` | low | 16,563 | 16,425 | 5 | 133 | 1,635 s | 1,644 / 3,042 MiB |
| `regression_heavy` | low | 1 (+1 collection skip) | 1 | 0 | 1 | 38 s | 2,383 / 2,397 MiB |
| `regression_medium` | **medium, not write-isolated** | 24 (+1 collection skip) | 24 | 0 | 1 | 42 s | 252 / 283 MiB |

The one extra skip in the heavy and medium partitions is the module-level
`tuf` import skip, which pytest reports in every run. So the full baseline
**does not pass**. 5 low-integrity failures remain:

- `test_legacy_tool_executor::test_verify_artifact_uses_packaged_acquisition_verifier`
  is deterministic. It is a false green on main. Issue #551.
- `test_managed_runtime_owner::test_full_manifest_owned_http_and_relaunch`
  hashes the whole venv. Issue #550.
- `test_app_recovery_http::test_http_original_pending_new_login_and_two_separate_approvals`
  failed with `RemoteDisconnected`. It takes 192 s even alone at medium
  integrity, so it is load-sensitive.
- `test_artifact_risk::test_partial_executable_does_not_parse_across_scan_gap`
  hit `TimeoutError` on its inspection deadline. It passed when rerun alone
  at low integrity.
- `test_install_workstation_local::test_installer_rejects_a_directory_that_is_not_a_sonder_checkout`
  hit a PowerShell `UnauthorizedAccess` `SecurityError`. It passed when rerun
  alone at low integrity.

The low partition took 1,635 s with 2 workers, so the 900-second nightly cap
is still not met at this worker count.

## Security review fix (BLOCK on `cb786816`)

The fixes, each covered by tests that failed against `cb786816`'s source and
pass now (10 new tests, red before and green after):

- **No candidate code at medium integrity.** `requires_medium_integrity`
  tests are excluded from both candidate partitions and recorded as not
  evaluated. Promotion of any run with unevaluated checks is blocked. The
  `integrity` parameter and the medium token were removed from
  `run_isolated()`.
- **Tested bytes are bound to promoted bytes.** Before the first gate, the
  parent records the SHA-256 of every changed file and of the diff. It
  re-derives both from disk before review, before the branch commit (and
  compares the committed blobs afterwards), and before deployment.
  `selfmod.deploy(..., expected_digests=...)` checks the candidate bytes
  before any copy and the installed bytes after it. Any mismatch fails
  closed.
- **Short scratch root.** The work directory lives under a short user-owned
  root (`%USERPROFILE%\.sl`, or `SONDER_SELFMOD_SCRATCH_ROOT`) and is refused
  above 48 characters. This addresses the `ERROR_DIRECTORY` (267) failures
  from deep pytest temp paths.
- **Worker count from commit headroom.** The default xdist worker count is
  sized from available commit charge (4 GiB per worker, keeping 25% in
  reserve) instead of a fixed 8.

### What the gates still do not prove

- Every verdict is the exit code of a process that runs candidate code. The
  held-out snapshot and digest manifest make evaluator-file tampering
  evident; they do not stop a candidate from trying to influence its own
  low-integrity pytest run.
- Low integrity bounds writes to medium-integrity objects. It does **not**
  bound reads or network access, so the confidentiality of anything the user
  can read is not covered.
- 24 tests are not evaluated against any unattended candidate. So no
  unattended candidate can be deployed without a human while those tests
  exist.

The full-baseline numbers above were measured at `1eb3ee25`. Because the
medium partition no longer runs against the candidate, its "24 passed" row
describes the trusted checkout only. It is not candidate evidence.
