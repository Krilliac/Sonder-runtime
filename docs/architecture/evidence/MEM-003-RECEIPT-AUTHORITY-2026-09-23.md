# MEM-003 receipt authority slice

Status: `implemented_unverified`.

## Correction (revision 6, security review of PR #525)

Revisions 4 and 5 of this document overclaimed. An independent review
found, and this revision fixes:

- **First-insert gate was not type-exact.** The document said exact internal
  types rejected duck-typed values at the first insert. In fact
  `SQLiteVerifierObservationRepository.append` only required a truthy
  `authorization.matches(...)`, so a stand-in object with `matches() -> True`
  inserted two forged trusted `passed` rows that `VerifiedSubjectFactPromotion`
  promoted. The gate now requires `type(authorization) is
  _ObservationAuthorization` (and exact receipt/observation types) before
  calling `matches`.
- **Independence was per lane, not per principal.** The key derived from the
  child lane id, so one principal running two child lanes over the same
  unchanged source reached `FACT`. The independence key is now
  `sha256({principal_id, workspace_scope})`; two lanes of one principal are a
  single source. Promotion therefore needs two distinct authenticated
  principals. In the app-control path the principal is the account
  (`account:sha256(username)`, `app_control_http._principal`), so
  independence is per account; additional accounts require admin
  registration or opt-in, so one account cannot mint a second independent
  source by itself.
- **Promotion loaded a global snapshot.** It read up to 10,001 rows across all
  projects under `BEGIN IMMEDIATE`; past that bound it raised "snapshot is
  incomplete" before the negative-evidence branch, so a new verified negative
  could not demote a contradicted fact. Promotion now reads only this
  project's rows for the exact subject token through an expression index
  (`verifier_learning_observations_subject`), and a separate negative query
  demotes before any completeness check.
- **The recovery refusal reason was stated wrongly.** The document claimed
  `PERSIST_VALUEERROR`. The producer now refuses `certified_after_return`
  explicitly with a `PermissionError` naming that phase, and the recorder
  reports the reason text.
- **The boundary was re-run twice per learning call.** Persistence re-ran
  `terminal_eligibility` (publication, inventory and manifest capture) and the
  producer's resolver re-ran it again while the work lease was held. The
  boundary now seals the exact decision it produced into its authority; the
  recorder passes that decision through, and the session binding check and
  the producer read the sealed snapshot without re-running the boundary.
- **Learning hook isolation.** Both hook call sites now catch every failure,
  including non-`Exception` types, so learning cannot alter a completed work or
  recovery outcome. Refusals (persistence or promotion) are recorded with a
  bounded reason and emitted as a structured warning log record.

## Design

`ReceiptObservationProducer` requires the opaque `_HostVerifierAuthority`
attached by the managed verifier boundary. That authority is a sealed
snapshot of the exact decision the boundary derived from the owner-bound
durable host turn at issue time; it does not re-read the turn later. The
producer derives worker, scope, subject digest, and outcome only from that
sealed value, so modified public fields on a copied eligibility are ignored
and a copy without the authority is refused. Because it is a snapshot, the
managed session that persists it first requires that the authority was
issued by that same session and that the sealed decision's host turn equals
the expected turn (`test_session_refuses_other_turn_or_other_session_decision`,
observed failing before the check was added).

`SQLiteVerifierObservationRepository` accepts a first insert only with the
producer's exact `_ObservationAuthorization` capability bound to the complete
receipt and observation payload. The capability is excluded from serialized
receipt data, so restart/replay compares the immutable persisted payload.

Promotion derives the reserved fact identity
`verified-subject-fact-<subject-digest>` and rejects caller-selected IDs. Under
`BEGIN IMMEDIATE` it first demotes on any persisted authenticated verified
negative for the subject, then reads the bounded subject-scoped set and fails
closed if it exceeds 10,000 rows or omits a selected observation.

The authority and insert capability are process-local Python objects. Exact
type checks reject duck-typed or caller-constructed public values, but
arbitrary code already running in the same Python process can inspect
underscore-prefixed module internals. This is an API and ownership boundary,
not a substitute for process isolation against a malicious extension.

## Live managed-work invocation

`sonder_runtime/bootstrap/managed_learning.py` adds `ManagedLearningRecorder`,
composed by `AppManagedWorkHttp` with the owned `Application` and the private
`runtime._standalone_verifier_factory`. It is invoked from
`AppManagedWorkDispatcher._run` after the host-current eligibility decision
and from `AppWorkRecoveryAttempt.resume` (composed by
`app_work_recovery_http`). It acts only on the exact authority type in a
certified/failed phase and on the exact managed owner types; callers pass no
receipt, worker, trust, or fact text, and no `HostFinalFacts` semantic claim
is accepted. When the composed unit of work has an authoritative fact source
whose project scope equals the receipt scope it runs
`MemoryLearningFacade.promote_verified_subject`; otherwise promotion reports
`unconfigured` or `out_of_scope`.

## Evidence (local, Windows, `-p no:cacheprovider`)

- `tests/test_receipt_observation_review.py`: 7 regression tests, each
  observed failing on the pre-fix code (7 failed) and passing after: duck-typed
  first insert (reviewer repro), same-principal two lanes stay `CANDIDATE`,
  distinct principals reach `FACT`, explicit `certified_after_return`
  refusal, verified negative demotes past both the global and same-subject
  bounds with the subject index in the query plan, single boundary resolution,
  and visible promotion refusal reason. The reviewer's `forge.py` now raises
  `PermissionError` on the first forged insert.
- `tests/test_app_recovery_coordinator.py::test_learning_hook_failure_never_escapes_recovery`:
  failed on the pre-fix coordinator (`KeyboardInterrupt` escaped), passes now.
  The dispatcher call site had no observable RED: `_unknown` already ignores
  terminal records, so its wrapper is defensive.
- `tests/test_managed_learning_composition.py::test_live_certified_managed_work_persists_authenticated_observation`:
  real `AppManagedWorkDispatcher`, `server._application()`, an
  `AppManagedAuthority`-bound `ManagedStandaloneSession`, and the real
  delegated verifier. The work reaches `terminal`/`certified`, exactly one
  `passed` observation is persisted with the host principal, run, final
  receipt digest and principal-derived independence key, and the boundary is
  resolved exactly once. Status is read with a freshly bounded selection so the
  fixture deadline cannot fail the test.
- `tests/test_app_recovery_coordinator.py::test_real_pending_work_explicitly_reattaches_and_certifies_once`:
  the live recovery hook runs for `certified_after_return` and is refused
  (`PERSIST_PERMISSIONERROR`, reason names `certified_after_return`), with no
  observation.

## Remaining gaps

`certified_after_return` recoveries are not learning evidence; live
promotion runs only when replication composition supplies an authoritative
fact source (PR #538 / issue #514); the verified-failed dispatcher path has
unit coverage but no live failing-check run; worker/model-class identity is
not part of independence (per account only); hosted CI has not run this
revision.

No semantic fact claim is accepted from `HostFinalFacts`; promotion is
limited to the canonical verifier-subject token.
