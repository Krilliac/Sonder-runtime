# Remaining #517 work: Linux uid-separated candidate evaluator (SELFMOD-002/003)

## Status

Implemented, **not verified** as a SELFMOD-002 or SELFMOD-003 requirement.
This slice adds an OS-enforced Linux boundary for unattended self-modification
candidate checks and an evaluator-held independent oracle that grades
candidates against expected values the candidate uid cannot read. It does not
close #517, and no master-spec checkbox changes. The former
`_UNATTENDED_ORACLE_INDEPENDENT = False` switch in `selfmod.py` is replaced by
a per-run check of an oracle receipt plus a fixed floor of unattended gates; see "When automatic promotion may
be enabled" below. The nightly driver still never promotes unattended.

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
| No privilege regain | `prctl(PR_SET_NO_NEW_PRIVS)`, set on the reaper before it launches anything and again in the candidate's pre-exec step before the uid drop, each verified with `PR_GET_NO_NEW_PRIVS`. The flag is inherited and cannot be cleared, so a setuid-root binary run by the candidate keeps the candidate's euid. The supervisor reads `NoNewPrivs: 1` from `/proc/<reaper>/status` before it allows the launch. |
| No network | While still root and before launching anything, the reaper calls `unshare(CLONE_NEWNET)` and checks that the new namespace has a different inode, contains only `lo`, and has `lo` down (`SIOCGIFFLAGS`). Loopback is deliberately not brought up, so the candidate has no route at all, including to `127.0.0.1`. The reaper reports the namespace and then blocks on a go pipe. The supervisor does not trust that report. It reads `/proc/<reaper>/ns/net`, which must differ from its own namespace and match the report, and `/proc/<reaper>/net/dev`, which must list only `lo`. Only after these checks does it release the reaper. The candidate's pre-exec step also checks that it is still in the confirmed namespace. The candidate cannot rejoin the host namespace, because that needs `CAP_SYS_ADMIN` over it. A namespace that cannot be created (for example `EPERM` without `CAP_SYS_ADMIN`) or that does not match these checks raises `LinuxIsolationUnavailable` and nothing launches. The run never falls back to the host network. |
| Process tree | A new session (`setsid`). Teardown goes by uid, not process group: the supervisor sends SIGKILL to every live process whose real uid is the candidate uid until none remain, so a descendant that called `setsid()` cannot escape. Teardown runs on timeout, on a job-memory breach, and as soon as the main candidate process exits. The reaper reaps orphans, so no zombies are left counting against RLIMIT_NPROC. |
| Bounds | `RLIMIT_AS` = `process_memory_mb`, `RLIMIT_NPROC` = `active_processes` (per real uid), `RLIMIT_CPU` = timeout + 5 s, `RLIMIT_FSIZE` = 1 GiB, `RLIMIT_CORE` = 0. `job_memory_mb` limits the summed RSS of all candidate processes. The supervisor enforces it by sampling every 100 ms, reported as `job_memory_enforcement: sampled-rss`. This is not atomic like a cgroup. |
| Exclusive uid claim | Before the spare-uid check, the supervisor takes an exclusive, non-blocking `flock` on `/run/sonder-selfmod-candidate/<uid>.lock` (directory root-owned, not group/other-writable) and holds it for the whole run. A second supervisor on the same uid fails closed instead of launching a candidate that could `ptrace` or signal the first run's candidate, or whose teardown would kill it. |
| Environment | The environment is replaced, not filtered. It contains a fixed `PATH`; locale/`TZ`; a private `HOME` and `TMPDIR`/`TMP`/`TEMP` (created by the supervisor, owned by the candidate uid, mode 0700, removed after the run); `PYTHONNOUSERSITE`; `PYTHONDONTWRITEBYTECODE`; and `PYTHONPYCACHEPREFIX`. `umask` is 077. |
| Evaluator truth | Before launch, each protected path must pass these checks, on both its literal and resolved ancestor chains: it is not a symlink; no ancestor directory is candidate-owned or candidate-writable (a sticky directory like `/tmp` is accepted only when the next entry is not candidate-owned); no POSIX ACL is present; and for a protected directory, no entry inside it is writable or a symlink. Any failure raises `ProtectedPathExposed` and nothing launches. After the run, the supervisor digests the bytes, ownership, mode and inode of each path and its ancestors again. Any change gives `integrity_failed: true` (exit code 2, not a pass). |
| Attestation | The supervisor builds the `job` report from the kernel wait status and its own observations: `integrity: "linux-uid"`, `uid`, `gid`, `supervisor_uid`, `limits`, `network` (`isolation: "netns"`, `netns_inode`, `supervisor_netns_inode`, `interfaces: ["lo"]`, `loopback_up: false`), `no_new_privs: true`, `exit`, `timed_out`, `limit_hit`, `peak_process_memory_mb` (VmHWM samples plus the reaper's `RUSAGE_CHILDREN` maxrss), `peak_job_memory_mb`, `peak_processes`, `lingering_processes_killed`. Candidate stdout never contributes to it. |

The following cases raise `LinuxIsolationUnavailable` before the candidate
launches: a non-Linux host; a non-root supervisor; no configured uid; a
uid/gid of 0 or equal to the supervisor's; a uid that already has live
processes (not spare); a kernel without `no_new_privs` or without `/proc`; a
kernel or Python without network namespaces; a reaper that cannot set
`no_new_privs`, create or verify its network namespace, become a subreaper or
launch the candidate; and a boundary the supervisor cannot confirm from
`/proc` within 10 seconds.
`ProtectedPathExposed` is a subclass of `LinuxIsolationUnavailable`.

### Selection and wiring

- `candidate_supervisor()` selects the Linux supervisor only on Linux, and only
  when `SONDER_SELFMOD_CANDIDATE_UID` names the dedicated uid.
  `SONDER_SELFMOD_CANDIDATE_GID` is optional and defaults to the uid.
  Otherwise the Windows low-integrity supervisor is selected. On a
  non-Windows host it still fails closed ("unsupported platform"; on Linux the
  refusal also names `SONDER_SELFMOD_CANDIDATE_UID` and this document). There
  is no default uid: `nobody` is shared, and RLIMIT_NPROC is per real uid.
- **Typed attestation.**
  `sonder_runtime/application/selfmod/candidate_isolation.py` defines
  `IsolationAttestation`. Both supervisors attach one to every `run_isolated`
  result (`attestation`), built from their own report:
  `selfmod_low_integrity.run_isolated` builds `kind="low"`, and
  `selfmod_linux_isolation.run_isolated` builds `kind="linux-uid"` with the
  candidate uid/gid and the supervisor uid. A `linux-uid` attestation cannot
  be constructed without a positive candidate uid that differs from the
  supervisor uid, and its pass flag must match the exit status. It also
  cannot be constructed without `network_isolated=True` and
  `no_new_privs=True`. `from_supervisor_result` sets these only from a report
  whose `network` names a `netns` inode different from the supervisor's,
  lists exactly `["lo"]` with `loopback_up: false`, and carries
  `no_new_privs: true`. Every consumer of a `linux-uid` attestation
  (`selfmod._record_command`, the host grader's clean replay and the nightly
  parent-scored gate) goes through this constructor, so a report without the
  network boundary is recorded as `unverified` (exit code 125).
- `selfmod._record_command` re-derives the attestation from the selected
  supervisor's report with `IsolationAttestation.from_supervisor_result`. It
  does not trust a supplied object; a supplied one must be equal. A report
  from the other supervisor, a missing or privileged uid, a contradictory pass
  flag, a `LinuxIsolationUnavailable`, or `integrity_failed` is recorded as
  exit code 125 with attestation `unverified`, and it never counts as a
  passing negative reproducer. The returned result carries the typed
  `attestation`. `record_host_grade` and the review eligibility check accept
  `{"low", "linux-uid"}` rows, and each of those rows can only be written by
  `_record_command`.
- `scripts/selfmod_host_grader.clean_replay` runs its replay probe through the
  selected supervisor and requires that supervisor's typed attestation. The
  fresh checkout's parent directory is made 0755 (root-owned) so the
  candidate uid can read, but not write, it.
- `scripts/selfmod_linux_isolation.py` and both of its test pins are
  `selfmod.protected_paths()` prefixes (tier 4).

### Nightly production path

`scripts/nightly_selfmod.run` (reached from `scripts/nightly_self_improve.py`
and `nightly_selfmod.main`) runs its candidates through the Linux supervisor
on a Linux host where `SONDER_SELFMOD_CANDIDATE_UID` is configured.

- Before any run, backup or workspace exists, `_isolation_refusal()` calls
  `selfmod_linux_isolation.candidate_isolation_preflight()`. The preflight
  checks for Linux, a root supervisor, `no_new_privs`, and a configured,
  unprivileged, spare uid. Without a configured uid the run returns
  "candidate isolation unavailable, no run started: Linux candidate isolation
  is not configured: set SONDER_SELFMOD_CANDIDATE_UID ... see
  docs/architecture/REMAINING-SELFMOD-517-LINUX-ISOLATION.md". When the uid
  supervisor is selected, the preflight also calls
  `require_not_candidate_writable([selfmod.database_path()])`. The selfmod
  database holds the baseline, tested digests and decisions, so it and its
  directory chain must be closed to the candidate uid.
- Every candidate gate (`syntax`, both regression partitions, `held_out`,
  `host_probe`, and the host grader's clean replay of the same bytes)
  receives `_evaluator_truth_paths(run_id, held_out)` as
  protected truth. That set is the held-out snapshot plus the sealed backup
  bundle and its manifest, which hold the rollback point and baseline hashes.
  The supervisor refuses to launch if any of it is candidate-writable, and
  re-digests it after the check. The database is not content-digested per
  gate, because the parent writes it between gates. The exposure check above
  and file permissions protect it instead.
- The held-out snapshot root is now 0555; `mkdtemp` created it 0700.
  `selfmod.apply_candidate_changes` keeps the replaced file's mode, or 0644
  for a new file, instead of `mkstemp`'s 0600. Both changes let the candidate
  uid read the bytes under test.
- `_parent_scored_gate` accepts a probe only through
  `accepted_probe_attestation(probe, selected_kind=...)`. The recorded
  isolation and the typed attestation must both equal the kind of the
  supervisor this host selected: `linux-uid` on a configured Linux host,
  `low` on Windows. Neither can vouch for the other.
- Each mutating stage goes through the bootstrap-composed selfmod stage
  journal. See [SELFMOD-001–006](REMAINING-SELFMOD-001-006.md).

### Independent oracle (result-frame forgery)

The public held-out suites, and the literal assertions the host grader
projects from them, are readable by the candidate uid. A candidate can print a
forged `SELFMOD HOST CHALLENGE RESULT` frame or pytest summary that carries
those public values. The independent oracle grades instead against cases the
candidate cannot read.

| Part | Where | What it does |
| --- | --- | --- |
| Held case set | `scripts/selfmod_oracle.py` | `<oracle home>/<module>.<function>.json`. The oracle home is `SONDER_SELFMOD_ORACLE_HOME`, which must be an absolute path. When it is unset, the home is `<selfmod state root>/oracle`, but only if no typed state home is configured in the process (`paths.configured_home()`). A runtime that composed `state.home` resolves the selfmod state root differently from a bare `python scripts/selfmod_oracle.py`. There the default is refused as unusable, the nightly reports that as not evaluated, and the variable must be set. The nightly logs the path it loaded, or the path it searched. The directory is `0700` and owned by the evaluator; each file is `0600`, written with `O_NOFOLLOW` and an atomic replace. Operators add cases with `python scripts/selfmod_oracle.py provision --module M --function F --cases FILE`, run with the nightly's environment. A case is `{"args", "kwargs", "expected"}` or `{"args", "kwargs", "raises": "ExcName"}`, 1 to 64 per set. A set is refused at provisioning or loading when its challenge (for any candidate root up to 4096 bytes) or its all-correct result frame (as the probe spells it) would exceed the 64 KiB channel bound. `inspect` prints the path, digest, case count and confidentiality, and never prints an expected value. |
| Confidentiality | `selfmod_linux_isolation.require_not_candidate_readable`, `selfmod_oracle.confidentiality` | On the `linux-uid` host, refuses a case file that the candidate uid can read. It also refuses a directory the candidate uid can list or traverse, a symlink, a POSIX ACL, and candidate ownership, and it applies `require_not_candidate_writable`. The Windows low-integrity supervisor does not bound reads, so there the set is never confidential. |
| OS-boundary proof | `selfmod_oracle.prove_read_denied` | Each run, runs evaluator-only code (`python -I`, so no candidate bytes) *as the candidate uid* through the supervisor. The probe must get `EACCES` when it opens the case file and when it lists the oracle home. Only a supervisor-attested `linux-uid` pass with both denials counts. |
| Channel | `selfmod_oracle.challenge_command`, `independent_oracle.new_challenge` | The parent picks a fresh 128-bit nonce, a fresh token per case and a random presentation order. The candidate probe receives only the nonce and `(token, args, kwargs)`. It prints one `SELFMOD ORACLE OUTPUTS` frame of raw outputs (a value, or the raised exception's class name) through the supervisor's output channel. |
| Ledger redaction | `independent_oracle.ledger_command` / `ledger_output`, applied by `selfmod._record_command` to every `oracle_probe` row | The challenge payload names the held inputs, and a passing frame *is* the held expected values. The ledger therefore stores the probe argv with its payload replaced by `{"nonce", "payload_bytes", "payload_sha256"}` and the output as `SELFMOD ORACLE WITHHELD {"bytes", "sha256"}`. Neither `/selfmod tests`, the hosted HTTP read surface, nor any other ledger read can build an input to expected-value table that a later candidate could replay by input. The journal stores only digests of the stage request and result. |
| Comparison | `independent_oracle.grade_frame`, called by `selfmod.record_oracle_grade` | Runs in the evaluator process on the parent's in-memory probe result. That result's argv and output must reproduce the redacted ledger row exactly, and its exit verdict must match the row. The argv must carry this challenge's nonce, the tested candidate bytes must be unchanged, and the case file must still have the digest it was loaded with. It accepts exactly one bounded frame that echoes the nonce and answers exactly this challenge's token set. Every raw output must equal the held outcome. Exit codes, pytest summaries and host-challenge frames are not inputs. The detail never repeats an input or an expected value. If grading cannot happen (for example, a challenge that cannot be built, a set re-provisioned mid-run, changed tested bytes or an unbound probe), `_independent_oracle_gate` returns a failed verdict. The nightly then rejects and discards the run instead of crashing with it left in `testing`. |
| Durable decision receipt | `independent_oracle.OracleReceipt`, table `selfmod_oracle_receipts` | A canonical JSON receipt stored with its SHA-256. The digest is unkeyed and sits in the same row, so it catches corruption and partial edits but is **not a seal**: a writer who can edit the row can recompute it. Receipt integrity rests on the ledger being closed to the candidate uid, which the nightly checks before every run (`_isolation_refusal`). It binds the verdict to the tested candidate digests (`files`, `diff_sha256`, re-derived from disk and required to equal the tested-bytes record), the baseline (`starting_commit` and the sealed backup `manifest.json` digest), the case-set digest, the nonce, the output digest, the probe row, and the probe's attestation kind and candidate/supervisor uids. `independent` is true only for a pass under `linux-uid` with the confidentiality check and the read-denial proof both passing. `oracle_receipt()` re-checks the digest and the row's `passed`/`independent` columns on every read. `record_test` refuses the `oracle_grade` kind, and a second receipt for a run is refused. |

The nightly driver (`scripts/nightly_selfmod.run`) loads the held set for the
target function before any candidate process runs. It adds the file to the
evaluator truth that every gate protects and re-digests. After the host grade,
it runs `_independent_oracle_gate` as a journaled `record_test` attempt
(`oracle_probe`). A failing oracle rejects the candidate. A missing held set,
or a verdict that is not independent (every non-Linux host), is passed to
`review` as `oracle_grade: ... NOT EVALUATED`.

#### When automatic promotion may be enabled

`selfmod.review` sets `auto_evaluation_eligible`, and `approve` accepts
`host:auto-low-risk`, only when **all** of the following hold:

1. The run is `auto-low-risk` mode, `low` risk, and `approval_required` is
   false.
2. There are no review failures and no unevaluated checks.
3. Every candidate check carries a supervisor-built isolation attestation.
4. Every kind in `selfmod.UNATTENDED_REQUIRED_KINDS` has a passing row:
   `syntax`, `regression`, `regression_heavy`, `held_out`, `host_probe`,
   `host_grade`, `oracle_probe` and `oracle_grade`. This floor is fixed. It
   applies whatever `require_kinds` the caller passes, so a narrow acceptance
   set cannot reach auto-approval.
5. Exactly one passing `host_grade` exists.
6. Exactly one `oracle_grade` exists, and its receipt passes
   `_oracle_admission_refusal`:
   - the recorded digest and columns agree with the receipt;
   - the verdict passed;
   - the attestation is `linux-uid`;
   - confidentiality and read denial were both proven;
   - the receipt is bound to the current tested bytes and the current
     baseline.

`approve` re-checks item 6 for every `host:` approver, so a receipt that is
removed, fails, or no longer matches the tested bytes or the baseline after
review is refused at approval time.

The nightly driver still never promotes unattended. It always records the
`regression_medium` partition as not evaluated, and it also requires
`oracle_independent`. A caller that drives the selfmod lifecycle itself,
under the configured Linux uid supervisor and with held cases for the target,
can now reach host approval. `tests/test_selfmod_independent_oracle.py`
demonstrates this. An operator may therefore enable `auto-low-risk` only on a
Linux host that meets all of these conditions:

- `SONDER_SELFMOD_CANDIDATE_UID` is configured and the supervisor runs as
  root;
- the oracle home is root-owned `0700` with `0600` case files, and
  `SONDER_SELFMOD_ORACLE_HOME` is set (absolute) whenever the runtime uses a
  typed `state.home`;
- the HTTP read surface and the ledger are not reachable by the candidate
  uid, since receipt integrity rests on ledger permissions (see "What
  remains");
- every target that should auto-promote has provisioned held cases whose
  values were never committed to the repository or left in a
  candidate-readable file.

On Windows, and on Linux without the uid, promotion always stops for a human.

### Operator-driven path (`/selfmod run`, `approve`, `deploy`, `rollback`)

The REPL console, the HTTP app chain (`sonder_serve._handle_slash`) and the
MCP `sonder` tool all reach `server.control_command`, which forwards
`/selfmod` to `server._selfmod_command`. Only the REPL passes
`operator_approved`, and only when a person is attached to the console
(`_console_has_operator()`).

- **Isolation selection.** `selfmod.operator_candidate_isolation` decides for
  every operator run. It calls `selfmod.candidate_isolation_refusal()`, the
  same preflight and ledger-exposure check the nightly driver uses
  (`nightly_selfmod._isolation_refusal` now delegates to it). With a usable
  supervisor the run is always isolated, whatever flags were given.
  Without one it raises `selfmod.CandidateIsolationRefused` (a
  `PermissionError` carrying the host `reason`). The only exception is an
  opt-in that meets all of these conditions:
  - the operator typed `--unisolated` for this command;
  - a console operator is attached;
  - `SELFMOD_LOW_INTEGRITY=1` is not set;
  - the run is not an `auto-low-risk` candidate.

  HTTP and MCP callers can never pass `operator_approved`. A written
  `/permissions` allow rule lets them reach `/selfmod run`, but it does not let
  them skip isolation. `_selfmod_command` checks isolation before it creates the
  run. `_execute_selfmod_run` checks it again, and that check is the one that
  counts, before any backup or workspace exists. That second check writes
  its decision as an `isolation` event in the run's audit trail. On refusal
  the surface returns
  `refused /selfmod run: candidate isolation unavailable, no candidate run
  started: ...`. On Linux, that message names `SONDER_SELFMOD_CANDIDATE_UID`
  and this document.
- **Isolated checks.** `_execute_selfmod_run` passes `low_integrity=True` for
  an isolated run and `False` for an opted-in unisolated one. The value goes
  to every `selfmod.record_test` (`syntax`, `targeted`, `regression`, and
  `security` for maintenance runs) and to `selfmod.record_smoke`, which now
  takes the same `low_integrity`/`protected_paths`/`isolation` arguments. Its
  `protected_paths` are `selfmod.evaluator_truth_paths(run_id)`: the sealed
  backup bundle and its manifest. Nightly builds its evaluator truth from the
  same function, adding its held-out snapshot. An unisolated run records its
  rows as `unverified`.
- **Stage journal.** `server._selfmod_stage_journal()` returns
  `_application().selfmod_service()`, the bootstrap-composed
  `GuardedLegacySelfmodService`. The following stages go through
  `journaled_stage`:
  - `/selfmod run`: `create_backup`, `prepare_workspace`,
    `record_reproducer_before`, `begin_testing`, every `record_test`,
    `record_smoke` and `review`. `record_smoke` is a new repeatable stage
    entry: `selfmod-record-smoke:<run>:attempt-<n>`, with success = `passed`.
  - `/selfmod approve`, `deploy` and `rollback`. The automatic rollback after
    a failed live reload is included.

  Without a composed journal the command refuses before it mutates anything.
  Before it composes a binding, `_selfmod_command` also refuses an unknown
  run id and a run in the wrong phase (`approve` needs `reviewing`, `deploy`
  `approved`, `rollback` `deployed`). It holds the run's ledger lease
  (`_selfmod_operator_lease`) across the stage, so a concurrent operator call
  on the same run is refused instead of fencing this one. The legacy
  `deploy`/`rollback` refusals made before any write (lock held, source
  changed, rollback conflict and others) settle as `failed` `:not-applied`
  outcomes and can be retried. See
  [SELFMOD-001–006](REMAINING-SELFMOD-001-006.md).
- The reproducer (`record_reproducer_before`) is journaled but not isolated.
  It runs the declared check against the untouched live source rather than
  candidate bytes, and `_record_command` already exempts it from
  auto-low-risk isolation.

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

The Linux boundary tests skip, with an explicit reason, unless the platform
is Linux and `euid == 0`. The `tests` job in `.github/workflows/ci.yml` runs
as the unprivileged runner user, so it skips them. The
`linux-selfmod-isolation` job in the same workflow runs
`tests/test_linux_candidate_isolation.py` and
`tests/test_wiring_selfmod_linux_nightly.py` under `sudo` with the
setup-python interpreter. It writes a JUnit report and fails if any test is
skipped, errors or fails. The required `tests` context needs this job to
succeed, as it does for `windows-focused` and `container-qualification`.
`tests/test_release_artifacts.py` pins that wiring. The evidence below
comes from a root container. The job itself has not yet run on a
GitHub-hosted runner, so hosted-runner qualification is not claimed until it
passes there.

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
  - Network and privilege canaries:
    - The candidate sees only `lo`, reports `NoNewPrivs: 1`, and runs in the
      namespace inode that the report attests. That inode differs from the
      supervisor's, and the supervisor stays in the host namespace.
    - The test binds TCP listeners on `127.0.0.1`, on every non-loopback
      IPv4 address the host has, and on `::1` where the kernel has IPv6. A
      host-side control first connects to each one. The candidate cannot
      connect to any of them, and none of them ever accepts a connection
      from it.
    - Name resolution (`getaddrinfo`) fails. TCP egress to public IPv4/IPv6
      addresses and a UDP datagram fail with an unreachable (or,
      without IPv6, unsupported-family) errno.
    - A root-owned setuid copy of `id` returns euid `0` in a control run as
      the candidate uid without `no_new_privs`, which proves the mount
      honours setuid. Under the supervisor it returns the candidate uid.
    - With `CAP_SYS_ADMIN` dropped from the bounding set, a root supervisor
      raises `LinuxIsolationUnavailable` ("network namespace unavailable")
      and the candidate never runs.
    - A reported namespace that is the supervisor's own, or that does not
      match what `/proc` shows, is refused.
- `tests/test_wiring_selfmod_linux_nightly.py` drives the real entry point,
  `scripts.nightly_selfmod.run`. It uses a real Git checkout, the real selfmod
  ledger, the real Linux supervisor and the bootstrap-composed stage journal.
  Only the model's proposal and its rewritten function are fixed.
  - (root) A dry cycle with `SONDER_SELFMOD_CANDIDATE_UID` set records
    `linux-uid` on every gate (`syntax`, `regression`, `regression_heavy`,
    `held_out`, `host_probe`, `host_grade`). The attestation names the
    candidate uid and supervisor uid 0. The parent-scored gate and the clean
    replay pass, and the stable checkout keeps its HEAD and bytes.
  - (root) A candidate tries to overwrite the live checkout, the base
    held-out suite and the ledger. It gets `Permission denied` and is
    rejected at the regression gate. The stable checkout is unchanged and
    still imports and behaves as before.
  - (any Linux) Without the uid, `run()` refuses before creating a run and
    names `SONDER_SELFMOD_CANDIDATE_UID` and this document.
- `tests/test_selfmod_operator_isolation.py` drives the operator path. The
  ledger, backup, workspace, checks and bootstrap-composed journal are real.
  Only the editing model is replaced.
  - (any host) The isolation decision: a host with a supervisor always
    isolates. A host without one refuses an unrequested opt-in, an
    unattended opt-in, an opt-in under `SELFMOD_LOW_INTEGRITY=1`, and an
    `auto-low-risk` run.
  - (any host) `approve`, `deploy` and `rollback` are journaled. The
    wrong-phase guard and the unknown-id guard admit no intent. Without a
    journal nothing mutates. A concurrent second operator call on a run is
    refused while the first completes. The real
    `server._selfmod_stage_journal()` composition drives a real `approve`.
  - (Linux) The real legacy `deploy` refused under a held deployment lock,
    and the real `rollback` refused on a conflict, both succeed when retried
    after the cause is cleared.
  - (any host) With a supervisor, every candidate check gets
    `low_integrity=True` and the backup bundle as protected truth.
  - (Linux, uid unset) The REPL refuses without `--unisolated`, and a piped
    console refuses with it. HTTP (`_handle_slash`) and MCP (`server.sonder`)
    refuse both forms even with an allow rule, and no run is created.
  - (Linux, uid unset) An attended `--unisolated` run reaches `reviewing`. Its
    rows are `unverified`, the opt-in is in the audit events, and every stage
    is journaled.
  - (root) A run with `SONDER_SELFMOD_CANDIDATE_UID` set records `linux-uid`
    on `syntax`, `targeted`, `regression` and `smoke`, naming the candidate
    uid and supervisor uid 0. Every stage is `completed` in the journal, and
    the live checkout is unchanged.
  - (root) A candidate that writes the live checkout on import gets
    `Permission denied` and is rejected, as a settled `failed` journal
    effect.
  - (root) The same isolated, journaled run driven through HTTP
    (`sonder_serve._handle_slash`) and MCP (`server.sonder`) with an allow
    rule. These surfaces need no attendance on a host with a supervisor. The
    test redirects only the repository root to the fixture checkout.
- `tests/test_wiring_selfmod_attestation.py` (any host) covers:
  - the typed attestation rules, including refusal of a `linux-uid` report or
    hand-built attestation without the network namespace boundary or
    `no_new_privs`;
  - cross-supervisor refusal;
  - the gate's binding to the selected supervisor kind;
  - the preflight message.
- `tests/test_517_linux_uid_separated_candidate_evaluator.py` (any host)
  covers:
  - the fail-closed errors for non-Linux, non-root, and missing or privileged
    identity;
  - platform-bound selection;
  - `selfmod` mapping every Linux fail-closed error to exit code 125;
  - acceptance of only supervisor-built `linux-uid`, and `selfmod` recording
    a report without the network/`no_new_privs` boundary as `unverified`;
  - refusal of a `low` claim from the Linux supervisor and of a `linux-uid`
    claim from the Windows one;
  - protected-path membership;
  - host-grade binding to a `linux-uid` probe;
  - `clean_replay` using the selected supervisor's attestation.
- `tests/test_selfmod_independent_oracle.py` covers the independent oracle.
  - (any host) The pure contract:
    - only correct raw outputs for this challenge pass;
    - a forged frame with guessed values, a forged pytest summary with a
      host-challenge frame, a verbatim replay from a previous nonce, a
      replay with the nonce rewritten, duplicate or oversized frames and
      non-finite or unencodable outputs all fail;
    - case sets and receipts are strict, and sets that cannot fit the
      channel are refused at provisioning;
    - the ledger forms of a probe carry only the nonce and digests;
    - only a `linux-uid` pass with a proven read denial is independent;
    - receipts are bound to candidate and baseline digests.
  - (POSIX) The store is `0700`/`0600`. It refuses symlinked, malformed and
    wrong-target sets.
  - (any host, low-report seam) A low-supervisor oracle pass is recorded but
    never independent and never auto-approves. Forging candidates are
    rejected by `review`. The probe's ledger row, as `/selfmod tests` serves
    it, holds no challenge payload or result frame. A probe issued for
    another challenge, an in-memory result that does not match its ledger
    row, and changed tested bytes are refused. A case set changed after
    loading is refused, and through the nightly gate that fails closed
    instead of raising. A partially edited receipt fails its digest check
    and refuses admission. A consistent rewrite of the whole row is accepted,
    which the test records as the limit of an unkeyed digest. `approve`
    re-checks the receipt at approval time: a missing, failed,
    other-baseline or other-bytes receipt, or changed tested bytes, is
    refused even though `auto_evaluation_eligible` is set. The oracle home
    resolves to the same directory for the operator CLI and the nightly, a
    typed state home without the explicit variable is refused, and a
    relative variable is refused.
  - (root) The real supervisor gives the candidate uid `PermissionError` on
    the expected-values file. `prove_read_denied` passes, and an exposed
    (`0644` in `0755`) set is refused by both checks.
  - (root) The real nightly entry point was run with these candidates:
    - A clean candidate advances (`COMMITTED`) with an independent receipt
      bound to its tested digests, the starting commit and the case-set
      digest, as the sixth journaled gate.
    - A forged-frame candidate and a forged-pytest-summary candidate pass
      every public gate (`regression`, `held_out`, `host_probe`,
      `host_grade`). Each is rejected with `candidate rejected: independent
      oracle failed`. Its attempt to read the held file appears in the
      parent's in-memory probe output as `[Errno 13] Permission denied`,
      and the ledger row is withheld.
    - Verbatim and nonce-rewritten replays of a previous clean run's frame
      are rejected.
    - The input-keyed replay from the review is also rejected. After a
      clean run, the attack rebuilds an input to output table from that
      run's `/selfmod tests` rows, finds nothing, and the candidate is then
      graded on its own wrong outputs.
    - An oracle that cannot grade (for example, a case set that changed
      mid-run) rejects and discards the run.
  - (root) `review(require_kinds={"syntax"})` auto-approves
    (`host:auto-low-risk`) under `linux-uid` only when the independent
    receipt exists and every unattended floor kind passed. The full gates
    without the receipt stop at `reviewing` with `no independent oracle
    receipt`. The receipt with only `syntax` and the grades stops with
    `unattended gates not passed: held_out, regression, regression_heavy`.

## What remains (do not claim #517 closed)

1. **Independent oracle: remaining limits.** The oracle described above is
   wired. What it does not cover:
   - **Held cases.** The oracle is only as independent as its held cases.
     Nothing mints them. An operator must provision them per target
     function, and values copied from repository tests are public. A target
     with no held set is not evaluated and blocks promotion.
   - **Probe detection.** The candidate can tell the oracle probe apart from
     other gates, because its payload carries tokens. It can therefore behave
     correctly only there. Like any test-based gate, the oracle grades
     behaviour on the held inputs, not behaviour elsewhere.
   - **Delegation.** A candidate may compute its outputs by calling the
     baseline code in the readable live checkout. That is still producing the
     outputs, not forging them.
   - **Confidentiality scope.** On Linux, confidentiality holds only against
     the candidate uid. It also depends on the host keeping the oracle home,
     the operator's provisioning input and the selfmod ledger unreadable to
     that uid. The ledger stores only digests of the held material, and the
     verdict detail never names it. Still, the candidate sees each held
     input in the clear on every run, and a correct candidate knows its own
     outputs. A candidate that smuggles those out through a channel it
     controls, such as a world-writable path or the network (item 2), can
     hand them to a later candidate. Every case is sent on every run,
     because sending a subset would let a wrong candidate pass on the cases
     left out.
   - **Receipt integrity.** The receipt digest is an unkeyed corruption
     check, not a seal. A process that can write the ledger can forge a
     consistent receipt. The ledger must stay closed to the candidate uid,
     and the nightly refuses to run otherwise. Any other principal with
     ledger write access is trusted.
   - **Windows.** No confidential oracle exists on Windows.
   - **Network.** Candidates run with no network (item 2), but path
     `AF_UNIX` sockets the candidate uid may open remain a channel.
   - **Nightly promotion.** The nightly driver still does not promote
     unattended, because `regression_medium` is never evaluated.
2. **Network isolation: limits of the namespace boundary.** Candidates now
   run with no network at all (see "No network" above). Two limits remain.
   The candidate can still reach host services through filesystem-path
   `AF_UNIX` sockets whose permissions admit its uid. Abstract `AF_UNIX`
   sockets belong to the network namespace, so those are cut off, but no
   Landlock or seccomp rule restricts path sockets. Also, a candidate test
   that needs loopback fails rather than being given a partial network. How
   many repository tests that affects is part of item 6.
3. **Confidentiality.** World-readable files, including held-out suites that
   the candidate must be able to read in order to run them, are readable by
   the candidate uid. Only write access is denied. The Sonder state home is
   `0711` on a host with `SONDER_SELFMOD_CANDIDATE_UID` configured (`0700`
   otherwise) and the SQLite stores opened through the connection factory
   (`memory.db`, `sessions.db`, ...), `goals.db` and the tool-audit JSONL are
   `0600`, so the candidate can traverse to its workspace under
   `selfmod/workspaces` without being able to list the home or read those
   stores. Other files in the home keep their own modes and are readable by
   name if they are world-readable. A home created `0700` before the uid was
   configured must be made `0711` by the operator once; no Sonder process
   re-closes a traverse-only home, even one started without the variable.
4. **Atomic job memory.** Job memory is sampled RSS, not a cgroup v2
   `memory.max`. A burst shorter than the 100 ms sampling interval can
   briefly exceed it. The per-process `RLIMIT_AS` still applies.
5. **Windows deployment documentation.** The Windows low-integrity baseline
   gaps recorded in
   [SELFMOD-002 low-integrity baseline](evidence/SELFMOD-002-LOW-INTEGRITY-BASELINE-2026-09-23.md)
   (MSYS2 runtime denial, Job memory limit for the QLoRA stack) and the
   operator deployment guide for the Windows path are still open.
6. **Full-baseline run on Linux.** The nightly parent-scored gate now accepts
   the `linux-uid` attestation (see "Nightly production path"). A dry nightly
   cycle runs end to end under the supervisor against a small real checkout.
   The full repository suite has not been run under the Linux supervisor.
   Regressions that appear only when the checkout is read-only to the test
   process are therefore not characterized yet. Tests that write next to
   their sources are one example.
7. **Kernel attack surface (partially addressed).** `no_new_privs` now blocks
   privilege gain through setuid/setgid binaries and file capabilities, and
   it is attested. The network namespace removes the host's network stack
   from reach. Still missing: no seccomp filter is applied, and unprivileged
   user namespaces stay available to the candidate if the host kernel allows
   them. A candidate could create its own user and network namespaces there.
   These do not change its host uid, so file permissions and uid-based
   teardown still apply, and they give it no route to the host network. They
   do widen the kernel surface it can reach. A seccomp filter would have to
   cover every syscall used by the Python test stack under evaluation, so it
   is not a small change and is not attempted here.
8. **CI qualification (wired; hosted run pending).** The
   `linux-selfmod-isolation` job in `.github/workflows/ci.yml` runs both root
   suites under `sudo` and refuses skips, and the required `tests` context
   depends on it. It has not yet been observed passing on a GitHub-hosted
   runner. Until it has, the evidence above is root-container evidence only.
   The job assumes that the runner's `/tmp` honours setuid (the canary's
   control run fails loudly if it does not) and that the toolcache
   interpreter can be read by an arbitrary uid.
9. **Operator-driven selfmod: residual limits.** The operator path now
   selects isolation and uses the stage journal (see "Operator-driven path").
   These limits remain:
   - An attended `--unisolated` run on a host without a supervisor executes
     the candidate with the operator's own privileges. That is the purpose of
     the opt-in, and the run's rows say `unverified`.
   - The console attests only that a person is attached
     (`_console_has_operator()`). A written allow rule for `selfmod` stops the
     REPL prompt, so at such a console the typed `--unisolated` is the only
     explicit consent.
   - Under the uid supervisor the checkout is read-only to the candidate, as
     for nightly. An operator's `--tests` command that writes next to its
     sources, or a regression run that needs `.pytest_cache`, degrades or
     fails.
   - `reject`, `cancel`, `resume`, `verify_backup` and the editing agent's
     guarded file tools still run outside the stage journal.
   - A `deploy`/`rollback` that fails before mutating for a reason other than
     the typed legacy refusals (for example an `OSError` on the backup
     manifest) still leaves its intent `uncertain`, and selfmod effects have
     no verifier to clear it. The run lease serializes operator calls, not
     operator calls against the nightly driver on the same run.
   - The item 8 CI job does not run
     `tests/test_selfmod_operator_isolation.py`'s root-only cases.

## Verification commands

```text
python -m pytest -q tests/test_linux_candidate_isolation.py            # Linux, euid 0 (CI: linux-selfmod-isolation, under sudo)
python -m pytest -q tests/test_release_artifacts.py                     # pins the CI job wiring
python -m pytest -q tests/test_517_linux_uid_separated_candidate_evaluator.py
python -m pytest -q tests/test_selfmod_independent_oracle.py              # root-only canaries: Linux, euid 0
python -m pytest -q tests/test_wiring_selfmod_linux_nightly.py tests/test_wiring_selfmod_attestation.py  # dry cycles: Linux, euid 0
python -m pytest -q tests/test_selfmod.py tests/test_selfmod_low_integrity.py tests/test_selfmod_isolation_scope.py
python -m pytest -q tests/test_selfmod_operator_isolation.py tests/test_selfmod_deploy_gate.py  # uid-supervisor cases: Linux, euid 0
python scripts/check_architecture.py
python scripts/check_error_signals.py
git diff --check
```
