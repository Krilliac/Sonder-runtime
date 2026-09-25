# Remaining #517 work: Linux uid-separated candidate evaluator (SELFMOD-002/003)

## Status

Implemented, **not verified** as a SELFMOD-002 or SELFMOD-003 requirement.
This slice adds an OS-enforced Linux boundary for unattended self-modification
candidate checks. It does not close #517, and no master-spec checkbox changes.
Automatic approval stays blocked by `_UNATTENDED_ORACLE_INDEPENDENT = False`
in `selfmod.py`.

## What exists

`scripts/selfmod_linux_isolation.py` exposes
`run_isolated(command, *, cwd, timeout, protected_paths, process_memory_mb,
job_memory_mb, active_processes, candidate_uid=None, candidate_gid=None)`.
It returns the same result schema as the Windows low-integrity supervisor
(`scripts/selfmod_low_integrity.py`): `exit_code`, a bounded `output` tail,
`passed`, `integrity_failed` when protected truth changed, and a `job`
attestation.

The supervisor must run as root. It starts a root **reaper** process
(`--reaper`) that registers as a child subreaper and dies with the supervisor.
The reaper launches the candidate. Candidate processes and their descendants
run under these controls:

| Control | Mechanism |
| --- | --- |
| Distinct identity | A dedicated unprivileged uid/gid. In the pre-exec step: `setgroups([])`, `setresgid`, `setresuid`. The code checks that `getresuid`/`getresgid` report the new ids and that `setresuid(0, 0, 0)` fails. |
| No privilege regain | `prctl(PR_SET_NO_NEW_PRIVS)`, set before the uid drop and verified with `PR_GET_NO_NEW_PRIVS`. |
| Process tree | A new session (`setsid`). Teardown goes by uid, not process group: the supervisor sends SIGKILL to every live process whose real uid is the candidate uid until none remain, so a descendant that called `setsid()` cannot escape. Teardown runs on timeout, on a job-memory breach, and as soon as the main candidate process exits. The reaper reaps orphans, so no zombies are left counting against RLIMIT_NPROC. |
| Bounds | `RLIMIT_AS` = `process_memory_mb`, `RLIMIT_NPROC` = `active_processes` (per real uid), `RLIMIT_CPU` = timeout + 5 s, `RLIMIT_FSIZE` = 1 GiB, `RLIMIT_CORE` = 0. `job_memory_mb` limits the summed RSS of all candidate processes. The supervisor enforces it by sampling every 100 ms, reported as `job_memory_enforcement: sampled-rss`. This is not atomic like a cgroup. |
| Exclusive uid claim | Before the spare-uid check, the supervisor takes an exclusive, non-blocking `flock` on `/run/sonder-selfmod-candidate/<uid>.lock` (directory root-owned, not group/other-writable) and holds it for the whole run. A second supervisor on the same uid fails closed instead of launching a candidate that could `ptrace` or signal the first run's candidate, or whose teardown would kill it. |
| Environment | The environment is replaced, not filtered. It contains a fixed `PATH`; locale/`TZ`; a private `HOME` and `TMPDIR`/`TMP`/`TEMP` (created by the supervisor, owned by the candidate uid, mode 0700, removed after the run); `PYTHONNOUSERSITE`; `PYTHONDONTWRITEBYTECODE`; and `PYTHONPYCACHEPREFIX`. `umask` is 077. |
| Evaluator truth | Before launch, each protected path must pass these checks, on both its literal and resolved ancestor chains: it is not a symlink; no ancestor directory is candidate-owned or candidate-writable (a sticky directory like `/tmp` is accepted only when the next entry is not candidate-owned); no POSIX ACL is present; and for a protected directory, no entry inside it is writable or a symlink. Any failure raises `ProtectedPathExposed` and nothing launches. After the run, the supervisor digests the bytes, ownership, mode and inode of each path and its ancestors again. Any change gives `integrity_failed: true` (exit code 2, not a pass). |
| Attestation | The supervisor builds the `job` report from the kernel wait status and its own observations: `integrity: "linux-uid"`, `uid`, `gid`, `supervisor_uid`, `limits`, `exit`, `timed_out`, `limit_hit`, `peak_process_memory_mb` (VmHWM samples plus the reaper's `RUSAGE_CHILDREN` maxrss), `peak_job_memory_mb`, `peak_processes`, `lingering_processes_killed`. Candidate stdout never contributes to it. |

The following cases raise `LinuxIsolationUnavailable` before the candidate
launches: a non-Linux host; a non-root supervisor; no configured uid; a
uid/gid of 0 or equal to the supervisor's; a uid that already has live
processes (not spare); a kernel without `no_new_privs` or without `/proc`; and
a reaper that cannot become a subreaper or launch the candidate.
`ProtectedPathExposed` is a subclass of `LinuxIsolationUnavailable`.

### Selection and wiring

- `candidate_supervisor()` selects the Linux supervisor only on Linux, and only
  when `SONDER_SELFMOD_CANDIDATE_UID` names the dedicated uid.
  `SONDER_SELFMOD_CANDIDATE_GID` is optional and defaults to the uid.
  Otherwise the Windows low-integrity supervisor is selected. On a
  non-Windows host it still fails closed ("unsupported platform"). There is no
  default uid: `nobody` is shared, and RLIMIT_NPROC is per real uid.
- `selfmod._record_command` accepts only the attestation that the selected
  supervisor builds. A Linux result must carry `integrity: "linux-uid"` and a
  positive `uid` that is not the supervisor's uid. A Windows result must carry
  `"low"`. Any other result, a `LinuxIsolationUnavailable`, or
  `integrity_failed` is recorded as exit code 125 with attestation
  `unverified`, and it never counts as a passing negative reproducer.
  `record_host_grade` and the review eligibility check accept
  `{"low", "linux-uid"}` rows, and each of those rows can only be written by
  `_record_command`.
- `scripts/selfmod_host_grader.clean_replay` runs its replay probe through the
  selected supervisor and requires that supervisor's attestation.
- `scripts/selfmod_linux_isolation.py` and both of its test pins are
  `selfmod.protected_paths()` prefixes (tier 4).

### Operator requirements

The candidate uid must be able to read the checkout (`cwd`), the Python
interpreter and its environment, but must not be able to write them. The
candidate checkout is owned by the supervisor. Candidate commands that need
to write inside the checkout, such as `.pytest_cache`, fail or degrade. If
the supervisor process is killed, the candidate's own descendants can outlive
it. The next run then refuses to start ("not spare") until an operator
removes those processes. The next run does not kill processes of a uid it
did not launch.

## Evidence

Evidence here is from a root container only. No CI qualification is claimed.
The Linux boundary tests skip, with an explicit reason, unless the platform
is Linux and `euid == 0`. Ordinary GitHub-hosted CI runs as a non-root user,
so it skips them.

- `tests/test_linux_candidate_isolation.py` (root only) covers these cases:
  - The candidate reports a distinct uid/gid, `groups=[]` and `NoNewPrivs: 1`.
  - Writing a protected file fails with `EACCES`, and its digest is unchanged.
  - Adversarial canaries:
    - held-out suite overwrite, append, truncate, chmod and utime;
    - unlink, rename, create and `os.replace` in the parent directory of a
      protected file;
    - swapping a symlink that points at the evaluator config;
    - a fork bomb above `active_processes`;
    - allocation above `process_memory_mb`;
    - a job-memory breach;
    - a timeout with a `setsid()` grandchild;
    - a clean exit that leaves a detached grandchild.

    Each is denied or killed, no candidate-uid process survives, and
    supervisor-side state is intact.
  - Pre-launch refusal for a world-writable ancestor, a candidate-owned
    ancestor or file, a world-writable file, a candidate-group-writable file,
    a symlinked protected path, and a writable entry inside a protected
    directory.
  - A forged stdout attestation does not change the report.
  - Pass and fail exit codes are reported correctly.
  - The environment is scrubbed and HOME/TMPDIR are private.
  - The post-exit re-digest detects changed truth.
  - `selfmod` records `linux-uid` from the real supervisor.
  - A second run on a uid another supervisor has claimed fails closed.
- `tests/test_517_linux_uid_separated_candidate_evaluator.py` (any host)
  covers:
  - the fail-closed errors for non-Linux, non-root, and missing or privileged
    identity;
  - platform-bound selection;
  - `selfmod` mapping every Linux fail-closed error to exit code 125;
  - acceptance of only supervisor-built `linux-uid`;
  - refusal of a `low` claim from the Linux supervisor and of a `linux-uid`
    claim from the Windows one;
  - protected-path membership;
  - host-grade binding to a `linux-uid` probe;
  - `clean_replay` using the selected supervisor's attestation.

## What remains (do not claim #517 closed)

1. **Independent oracle (result-frame forgery).** The candidate still produces
   the output that the parent grades. It can read the public tests and print a
   forged `SELFMOD HOST CHALLENGE RESULT` frame or pytest summary.
   Process/uid separation does not stop this. Automatic promotion stays
   disabled until the expected values are held and compared by a process the
   candidate cannot influence.
2. **Network isolation.** The candidate uid keeps the host's network access.
   Neither a network namespace nor an egress policy is applied.
3. **Confidentiality.** World-readable files, including held-out suites that
   the candidate must be able to read in order to run them, are readable by
   the candidate uid. Only write access is denied.
4. **Atomic job memory.** Job memory is sampled RSS, not a cgroup v2
   `memory.max`. A burst shorter than the 100 ms sampling interval can
   briefly exceed it. The per-process `RLIMIT_AS` still applies.
5. **Windows deployment documentation.** The Windows low-integrity baseline
   gaps recorded in
   [SELFMOD-002 low-integrity baseline](evidence/SELFMOD-002-LOW-INTEGRITY-BASELINE-2026-09-23.md)
   (MSYS2 runtime denial, Job memory limit for the QLoRA stack) and the
   operator deployment guide for the Windows path are still open.
6. **Nightly wiring and full-baseline run on Linux.** `scripts/nightly_selfmod.py`
   (protected) still describes only the Windows supervisor, and its
   parent-scored gate (`_parent_scored_gate`) still requires a host probe with
   isolation `"low"`. On Linux a `linux-uid` probe therefore fails that gate
   (fail closed); the nightly parent-scored path cannot pass on Linux until
   that protected file accepts the selected supervisor's attestation. The full
   repository suite has not been run under the Linux supervisor.
7. **Kernel attack surface.** No seccomp filter is applied, and unprivileged
   user namespaces stay available to the candidate if the host kernel allows
   them. They do not change the candidate's host uid (so file permissions and
   uid-based teardown still apply), but they widen the kernel surface the
   candidate can reach.
8. **CI qualification.** A root-capable Linux CI job for
   `tests/test_linux_candidate_isolation.py` does not exist yet.

## Verification commands

```text
python -m pytest -q tests/test_linux_candidate_isolation.py            # Linux, euid 0
python -m pytest -q tests/test_517_linux_uid_separated_candidate_evaluator.py
python -m pytest -q tests/test_selfmod.py tests/test_selfmod_low_integrity.py tests/test_selfmod_isolation_scope.py
python scripts/check_architecture.py
python scripts/check_error_signals.py
git diff --check
```
