# MEM-003 receipt authority slice

Status: `implemented_unverified`.

This slice closes the first-insert and public-value forgery holes in the
receipt observation path. `ReceiptObservationProducer` now requires the
opaque authority attached by the managed verifier boundary and resolves that
authority against the current owner-bound terminal decision before deriving
worker identity, scope, subject digest, and outcome. A copied or modified
`ManagedTerminalEligibility` without that authority is refused, and modified
public fields on a copied value are ignored in favor of the durable resolver.

`SQLiteVerifierObservationRepository` accepts a first insert only when the
producer's in-process authorization capability matches the receipt and
observation. The capability is excluded from serialized receipt data, so
restart/replay still compares the immutable persisted payload. Contradictory
and failed verifier receipts remain negative learning evidence; this slice
does not accept caller-authored semantic fact text. The live application
composition now exposes a managed-session durable persistence method using the
application-owned unit of work, and `MemoryLearningFacade.promote_verified_subject`
can promote the canonical `verified-subject:<digest>` token after the learning
ladder has two independent authenticated workers; a persisted verified
negative demotes the same fact.

Promotion derives the reserved fact identity as
`verified-subject-fact-<subject-digest>` and rejects caller-selected IDs. It
also takes a `BEGIN IMMEDIATE` snapshot, loads the complete bounded
observation set, and evaluates every persisted observation for the subject;
omitted contradictory receipts therefore cannot be bypassed. An incomplete
snapshot fails closed.

The authority and insert capability are process-local Python objects. Exact
internal types reject ordinary duck-typed or caller-constructed public values,
but arbitrary code already running in the same Python process can inspect
underscore-prefixed module internals. This is an API and ownership boundary,
not a substitute for process isolation against a malicious extension.

Evidence:

- `tests/test_receipt_observation.py`: 17 focused tests, including public
  eligibility forgery, modified-field substitution, first-insert forgery,
  restart/replay, and immutable conflict behavior.
- `tests/test_managed_terminal_eligibility.py`: 6 focused tests, including the
  live failed-verifier authority attachment.
- `tests/test_receipt_observation.py`: production unit-of-work composition,
  independent subject promotion, and persisted negative demotion.

## Live managed-work invocation (revision 5)

`sonder_runtime/bootstrap/managed_learning.py` adds `ManagedLearningRecorder`,
a bootstrap-owned recorder composed by `AppManagedWorkHttp` with the owned
`Application` and the same private `runtime._standalone_verifier_factory`
that decides terminal eligibility. It is wired into the two production
points where a managed session reaches a terminal verifier outcome:

- `AppManagedWorkDispatcher._run`, after the host-current eligibility
  decision is recorded (certified terminal or verified failed check);
- `AppWorkRecoveryAttempt.resume`, after recovered completion or a verified
  failed check (composed by `app_work_recovery_http`).

The recorder only acts on a `ManagedTerminalEligibility` carrying the exact
`_HostVerifierAuthority` type in a certified/failed phase, and only for the
exact `ManagedConversationLifetime`/`ManagedStandaloneSession` owner types.
It calls `persist_learning_observation_durable`, which re-runs the current
owner-bound eligibility and the receipt producer against the application
unit of work; callers pass no receipt, worker, trust, or fact text, and no
`HostFinalFacts` semantic claim is accepted. Failures are recorded as a
bounded `refused` outcome and never change the work result. When the
composed unit of work has an authoritative fact source whose project scope
equals the receipt scope, the recorder then runs
`MemoryLearningFacade.promote_verified_subject` for the canonical subject
token; otherwise promotion reports `unconfigured` or `out_of_scope`.

Evidence (local, Windows, `-p no:cacheprovider`):

- `tests/test_managed_learning_composition.py::test_live_certified_managed_work_persists_authenticated_observation`:
  real `AppManagedWorkDispatcher`, `server._application()`, an
  `AppManagedAuthority`-bound `ManagedStandaloneSession`, the real delegated
  verifier and approval bridge. The work reaches `terminal`/`certified`, and
  exactly one `passed` observation is persisted in the application memory
  database with the host principal, run, final receipt digest, and
  worker-derived independence key. No eligibility value is constructed by
  the test. RED check: disabling the dispatcher hook makes this test fail.
- `tests/test_app_recovery_coordinator.py::test_real_pending_work_explicitly_reattaches_and_certifies_once`:
  the live recovery hook runs for a `certified_after_return` completion and
  fails closed (`refused`, `PERSIST_VALUEERROR`, no observation) because the
  original outward final carried no certificate identity.

Remaining gaps: `certified_after_return` recoveries are not yet learning
evidence (the producer requires the original final to carry the
certificate); live promotion runs only when replication composition supplies
an authoritative fact source (owned by PR #538 / issue #514); the
verified-failed dispatcher path is covered by unit tests, not a live
failing-check run; and hosted CI has not yet run this revision.

No semantic fact claim is accepted from `HostFinalFacts` in this slice;
promotion is limited to the canonical verifier-subject token.
