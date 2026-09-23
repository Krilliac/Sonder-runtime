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
