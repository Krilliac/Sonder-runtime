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
receipt, worker, trust, or fact text. The only semantic claim is one sealed
into the host final facts by a host verifier (see "Explicit host fact claims"
below). When the composed unit of work has an authoritative fact source
whose project scope equals the receipt scope it runs
`MemoryLearningFacade.promote_verified_subject`, or
`MemoryLearningFacade.promote_verified_claim` for a receipt that carries a
claim digest; otherwise promotion reports `unconfigured` or `out_of_scope`.

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

Ordinary receipts carry no fact text and still promote only the canonical
verifier-subject token. Semantic fact text is accepted only through the
explicit host claim bridge below.

## Explicit host fact claims (ISSUE-513 claim slice)

`HostFinalFacts` has two optional fields, `verified_fact_claim` and
`verified_fact_claim_digest`, that only a host verifier may populate. They
must be supplied together, the claim is bounded to 512 characters (the
`LearningObservation.content` bound), the digest must be the SHA-256 of the
exact UTF-8 claim, and a `verified-subject:` token is refused as a claim.
When both are absent they are left out of the sealed final receipt, so the
final binding digest of every claim-free turn is unchanged.

`ReceiptObservationProducer` reads the claim only from the sealed decision
inside the verifier authority. With a claim it re-checks the digest (fails
closed with `PermissionError` on a mismatch), uses the claim as the
observation content, stores `claim_digest` on the `VerifierReceipt`, and adds
`claim_digest:<digest>` to the observation provenance. Both are covered by the
producer's insert capability. A receipt without a claim serializes exactly as
before (`claim_digest` is omitted, not null), so existing rows replay byte for
byte.

`ReceiptBoundFactPromotion`
(`sonder_runtime/application/memory/receipt_bound_fact_promotion.py`, exposed
as `MemoryLearningFacade.promote_verified_claim`) writes the claim text as a
fact with the same rules as subject promotion: the fact identity is reserved
(`verified-claim-fact-<sha256(project, claim digest)>`) and an occupied
identity is refused; the caller must hold `BEGIN IMMEDIATE`; every selected
observation must be scoped to the project, authenticated, trusted, and bound
to the requested claim by both content and receipt digest; a persisted
authenticated verified negative for the claim demotes (tombstones) before any
completeness check; and positive promotion reads the complete project- and
claim-scoped snapshot (at most 10,000 rows) and needs two distinct
authenticated principals. Caller-supplied fact metadata is not accepted.
`ManagedLearningRecorder` routes a persisted receipt that carries a claim
digest to this path and every other receipt to subject promotion.

Evidence: `tests/test_receipt_bound_fact_promotion.py` (producer binding,
digest refusal, claim-free serialization, `HostFinalFacts` bounds, composed
promotion by two principals, same-principal non-promotion, verified-negative
demotion with tombstone, subject receipts cannot promote text, reserved and
occupied identities, snapshot transaction required, forged claim digest,
scope and relabeled-row refusal, and recorder routing end to end) and
`tests/test_host_final_receipt.py` (claim round trip through the sealed final
receipt; claim-free receipts keep their shape).

Remaining gap: no live host verifier populates `verified_fact_claim` yet
(`server.py` builds `HostFinalFacts` without it), so the live path is
reachable only once a host-owned claim source exists. Snapshot reads match the
exact claim text; a contradiction phrased with different case or whitespace is
a separate claim.
