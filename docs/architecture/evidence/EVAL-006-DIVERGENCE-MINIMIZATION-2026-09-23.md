# EVAL-006: earliest meaningful divergence and minimized reproducible failures

EVAL-006 requires identifying the earliest meaningful decision divergence
and retaining minimized reproducible failures. Audit context:
[`EVAL-AUDIT-2026-09-23.md`](EVAL-AUDIT-2026-09-23.md).

**Status: `implemented_unverified`.** The capability exists as an
application-layer contract with a durable adapter, but it has no production
caller: `EvaluationApplicationService` is not composed in the bootstrap graph,
and EVAL-005 (replay of recorded sessions with side-effect substitution) is
still open. The master specification says a feature is not complete merely
because a class or test double exists, so a revision 4 `verified` record was
superseded by revision 5 after independent review of pull request #546.

## What changed

- `sonder_runtime/application/evaluation/divergence.py`
  - `DivergencePolicy` compares step `output` as the decision content,
    optionally keeps only named `decision_paths`, and drops `ignored_paths`
    (latency, timestamps, request IDs). `state` is deliberately not a
    decision field: replay carries recorded step state into the candidate,
    so a state comparison could never observe a candidate difference.
  - `earliest_divergence` / `replay_divergence` return the first step whose
    projected decision content differs, with bounded changed paths; a length
    mismatch diverges at the first missing index.
  - `minimize_failure` reduces a divergent replay. Recorded outputs are only
    a valid oracle for the exact sequence that produced them, so without a
    baseline the result is the shortest reproducing **prefix**; with a
    `baseline_factory` the oracle is **differential** (fresh baseline versus
    fresh candidate on each subset) and ddmin reduces the preceding context.
    Minimization is anchored to the originally reported divergence: the
    divergent step is always retained last, and a trial counts only if it
    diverges first at that step with the same projected expected and actual
    content, so removing context cannot swap in an unrelated bug.
    `max_evaluations` is a hard ceiling that includes the baseline check, the
    initial replay, and both confirmation replays.
  - `MinimizedFailure` digests are integrity checks, not tamper-proofing
    (plain SHA-256 that an editor can recompute). Construction and loading
    also require the divergence to sit on a policy decision field at the last
    retained step, with an expected digest matching that stored step.
- `sonder_runtime/adapters/evaluation_failure_corpus.py`:
  `JsonMinimizedFailureStore` writes `<sha256>.json` under an exclusive
  `O_CREAT | O_EXCL` lock holding a random per-acquisition token (capacity
  check, no-overwrite check, and atomic rename in one critical section).
  Release deletes the lock only if it still holds this writer's token. A
  stale lock is broken by an atomic rename to a unique sidecar, and is kept
  if the sidecar turns out to hold a newer holder's token. Abandoned
  temporary files are removed by age, and records are re-verified on load.
- `sonder_runtime/application/evaluation/service.py`:
  `earliest_divergence`, `minimize_and_retain_failure`, `retained_failures`,
  and `reproduce_retained_failure`; the store is injected through the
  `MinimizedFailureStore` port.

## Evidence

`tests/test_eval006_divergence_minimization.py` (20 tests, deterministic
in-process fakes only — no model):

- Noise versus decision: raw comparison diverges at step 0 on
  `latency_ms`; under the policy the earliest divergence is step 7 on `y`.
- A stateful key-value session whose candidate truncates values after a mode
  switch diverges at step 6 of 10; differential minimization returns source
  steps `(0, 3, 6)`, marked 1-minimal, and an independent check confirms each
  retained step is necessary.
- With a second, unrelated bug present (missing keys answer a sentinel),
  minimization still returns `(0, 3, 6)` and the original divergence digests;
  before the anchoring fix it returned `(5,)`, a different bug.
- The evaluation ceiling holds at budgets 4, 5, 8, and 64 (it previously
  reached 6 against a budget of 4), and budgets below the fixed replays are
  refused.
- Loading refuses a divergence whose expected digest or index contradicts the
  stored steps even when the record digest is recomputed.
- `state` is refused as a decision field.
- Prefix strategy, unfaithful baseline, non-divergent and nondeterministic
  candidates, JSON round trip, tampering, bounded stores, stale temporary
  cleanup, held-lock timeout, and service-level retain/reproduce are covered.
- Lock ownership: release leaves another writer's lock in place; a stale-lock
  break leaves a fresh holder's lock in place; a crashed writer's stale lock
  is broken. Six concurrent writers against a capacity of four retain exactly
  four. This test first failed 2 of 3 runs on Windows, where a waiter reading
  the lock made the holder's unlink fail silently and leak the lock. Waiters
  now read the token only once a lock is stale, and release retries the
  unlink for a bounded time.

Each review fix was preceded by a test that failed on the previous code.

## CI record

Pull request #546's CI run
[`35933879745`](https://github.com/Krilliac/Sonder-runtime/actions/runs/35933879745)
passed for the earlier head `54b54207a4c2e29b86a5ec67da43b0fefba9a0da` (full
suite 16483 passed, 104 skipped). That run predates the review fixes above
and is recorded as CI history only; it is not a verification of EVAL-006.

## Limitations

- No production caller: the service is not composed in the bootstrap graph,
  and no operator command or live replay path minimizes or retains failures.
- No live model session was replayed; all evaluators are deterministic fakes.
  A nondeterministic evaluator is refused rather than minimized.
- Differential minimization requires the caller to supply baseline behavior;
  with only a recording, minimization is limited to the reproducing prefix.
- Meaningfulness is whatever the caller's `DivergencePolicy` declares.
- The file lock is advisory and assumes cooperating writers on one host; a
  lock older than 60 seconds is presumed abandoned.
