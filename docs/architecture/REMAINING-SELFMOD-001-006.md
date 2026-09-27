# Remaining self-modification governance (SELFMOD-001–006)

## Scope

This slice closes the remaining application-level governance gap around
self-modification candidates. It is intentionally isolated from the existing
`SelfModificationService`: the existing service owns the legacy lifecycle,
while `SelfmodGovernance` supplies an evidence-first authorization boundary
for new callers.

## Contract

`SelfmodGovernance` records the following lifecycle in order:

1. propose a candidate with a baseline digest;
2. attach externally supplied worktree metadata;
3. record an independently produced verification result;
4. record a review that cites known verification evidence;
5. approve only after the preceding gates; and
6. emit a local deployment intent.

The module is persistence-neutral and has no filesystem, subprocess, git,
network, deployment, or remote-push side effects. Worktree metadata describes
an adapter result; governance does not create or remove the worktree.

## Gate semantics

- Guarded candidates require an isolated, clean, adapter-managed worktree, a passing
  verification, and an approving review citing that verification.
- A failed guarded gate rejects the candidate and cannot be converted into an
  approval through a later call.
- A review cannot cite unknown evidence IDs.
- `unrestricted=True` is an explicit boundary, not a synthetic pass. It
  permits the caller to proceed with non-isolation, failed verification, or
  rejected review, while recording each bypass on both the candidate and the
  deployment intent. It still requires the lifecycle records to exist, so
  missing evidence is not silently represented as passed evidence.
- Deployment intent always has `automatic_push=False` and
  `remote_push_allowed=False`; requesting automatic remote push is refused.
  Actual deployment remains an explicitly separate executor concern.

## Journaled legacy stages on the nightly path

`GuardedLegacySelfmodService` journals each legacy mutating stage under its
own worker-effect identity. The stages are `create_backup`,
`prepare_workspace`, `record_reproducer_before`, `begin_testing`,
`record_test`, `review`, `approve`, `deploy` and `rollback`. Repeatable
stages get per-attempt identities derived from the durable journal
(`selfmod-record-test:<run>:attempt-<n>`), and a retry refuses while an
earlier attempt is unresolved.

The unattended nightly driver now uses those identities in production:

- `GuardedLegacySelfmodService.journaled_stage(run_id, stage, request, invoke)`
  applies the same identity, phase precondition and success predicate to a
  legacy call that the host driver makes itself. It raises `Forbidden` when no
  effect binding factory is composed, and `InvalidInput` for an unknown stage.
  It never runs a stage unjournaled.
- `scripts/nightly_selfmod.run` gets the stage journal from
  `_compose_stage_journal()`, which returns `default_app().selfmod_service()`.
  That is the bootstrap service whose binding factory is
  `_compose_selfmod_binding` over the shared worker-effects journal. If the
  service cannot be composed, the run refuses before it creates anything.
- The driver routes these calls through `journaled_stage`: `create_backup`,
  `prepare_workspace`, `begin_testing`, every candidate gate (`record_test`,
  including the parent-scored `host_probe`), `review`, `approve` and `deploy`.
  A failed gate is a settled `failed` outcome. An exception leaves the intent
  `uncertain`, so the run needs reconciliation before that stage can be
  retried.

The operator path (`server._selfmod_command` and `_execute_selfmod_run`,
reached from the REPL, HTTP and MCP) now journals through the same bootstrap
service. `server._selfmod_stage_journal()` returns
`_application().selfmod_service()`. These stages go through `journaled_stage`:

- `/selfmod run`: `create_backup`, `prepare_workspace`,
  `record_reproducer_before`, `begin_testing`, every `record_test`, the new
  repeatable `record_smoke` stage (`selfmod-record-smoke:<run>:attempt-<n>`)
  and `review`;
- `/selfmod approve`, `deploy` and `rollback`, including the automatic
  rollback after a failed live reload.

Without a composed journal each of these refuses before it mutates anything.
Before it composes a journal binding, the operator command also refuses an
unknown run id, a run already held by another `/selfmod` call and a run in
the wrong phase (`approve` needs `reviewing`, `deploy` `approved`, `rollback`
`deployed`). None of these refusals writes a journal record.

- **One operator call per run.** Composing a selfmod binding treats any open
  intent of the run as orphaned. Two concurrent operator calls on one run
  (a double submit, or the REPL and an HTTP caller) would otherwise fence each
  other mid-effect. `server._selfmod_operator_lease` holds the run's ledger
  lease (`selfmod.claim`, one live owner per run across threads and
  processes, renewed by a heartbeat) from before the binding is composed
  until the command returns. For `/selfmod run` it covers every stage from
  the backup on. A second caller gets `refused /selfmod <action>: run <id> is
  already being driven by another /selfmod call`.
- **Benign refusals are retryable.** `deploy` and `rollback` still admit
  their one-shot intent before the legacy body runs. The legacy module now
  raises `SelfmodStageNotApplied`
  (`sonder_runtime/application/selfmod/stage_refusal.py`, a `RuntimeError`)
  for the refusals it makes before writing anything:
  - `deploy`: the phase check, the deployment lock held by another
    deployer, the source tree changed since the proposal, a failed backup
    verification, a diff outside the approved scope, a missing tested-bytes
    record, and candidate bytes that differ from the tested bytes;
  - `rollback`: the phase check, the deployment lock, and "rollback
    conflict: deployed files changed after deployment".

  `GuardedLegacySelfmodService._mutating_call` settles such a refusal as a
  `failed` outcome whose receipt ends in `:not-applied`, then re-raises it.
  A later call of a one-shot stage gets a new identity
  (`<operation>:<run>:retry-<n>`, receipt `selfmod:<run>:<stage>:retry-<n>`)
  only while every earlier call of that stage settled `:not-applied`. A
  completed call, a `failed` call that ran, and an in-flight or `uncertain`
  call keep the original identity, so the journal refuses the duplicate as
  before. Any other exception, including a failure after the first live
  byte was replaced, still leaves the intent `uncertain`.

Candidate isolation on this path is described in
[#517 Linux isolation](REMAINING-SELFMOD-517-LINUX-ISOLATION.md).

What remains:

- `verify_backup`, `record_host_grade`, `reject`, `cancel` and `resume`
  still run outside the journal. None of them has a stage entry or success
  predicate.
- Only the legacy refusals listed above are typed as not applied. A
  `deploy`/`rollback` that fails for any other reason before mutating (for
  example an `OSError` reading the backup manifest) still leaves the intent
  `uncertain` and fences the run.
- The run lease serializes operator calls only. The nightly driver does not
  claim its runs, so an operator stage and a nightly stage on the same run at
  the same time are still not serialized against each other.
- Self-mod operation families have no provider verifier. An `uncertain`
  stage can be cleared only by future trusted reconciliation.

## Evidence

`tests/test_wiring_selfmod_linux_nightly.py` runs `nightly_selfmod.run` with
the stage journal from `build_application(...).selfmod_service()`. It then
reads the journal back:

- backup, workspace, `begin_testing` attempt 1 and review are `completed`;
- there are five contiguous `record_test` attempts;
- a rejected candidate's failing gate is a settled `failed` effect.

`tests/test_selfmod_operator_isolation.py` reads the journal back after an
operator `/selfmod run`. Backup, workspace, reproducer, `begin_testing`,
three contiguous `record_test` attempts, `record_smoke` and review are all
`completed`. The same file shows that `approve`, `deploy` and `rollback` are
journaled and that the wrong-phase guard admits no intent. Its root-only case
also shows that a rejected candidate is a settled `failed` effect. It also
covers:

- the production composition: the real `server._selfmod_stage_journal()`
  over a hermetic state home, driving a real `approve`;
- a concurrent second `deploy` refused while the first completes, and a
  rollback afterwards;
- a run leased by another owner, refused before any journal record;
- unknown or overlong run ids, refused without a journal record;
- the real legacy `deploy` refused under a held deployment lock and the real
  `rollback` refused on a conflict, each retried after the cause is cleared
  (Linux);
- an untyped failure, which still fences the run.

`tests/test_guarded_selfmod_stage_effects.py` covers the `:not-applied`
settlement and the `:retry-<n>` identities at the service level. It shows
that a completed deploy, or a failed one that ran, still refuses a second
call.
`tests/test_selfmod_deploy_gate.py` gives each test its own bootstrap-composed
journal.
`test_production_stage_journal_is_the_bootstrap_selfmod_service` checks that
the default composition returns the bootstrap service.
`tests/test_wiring_selfmod_attestation.py` checks that `journaled_stage` fails
closed. `tests/test_wiring_selfmod_compute_cancel_attempts.py` drives the
companion compute-cancel attempt identities through the bootstrap-composed
compute worker and the HTTP facade (`dispatch_compute_job_cancel`).

`tests/test_remaining_selfmod_governance.py` covers guarded ordering,
worktree isolation and cleanliness metadata, failed verification and review,
evidence references, unrestricted bypass reporting, lifecycle completeness,
remote-push refusal, and intent idempotence.

Focused verification commands:

```text
python -m pytest -q tests/test_remaining_selfmod_governance.py
python scripts/check_architecture.py
python scripts/check_requirement_evidence.py
python -m compileall -q sonder_runtime
git diff --check
```

No formal checklist checkboxes are changed by this slice.
