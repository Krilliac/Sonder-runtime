# EVAL-006: earliest meaningful divergence and minimized reproducible failures

EVAL-006 requires identifying the earliest meaningful decision divergence
and retaining minimized reproducible failures. Audit context:
[`EVAL-AUDIT-2026-09-23.md`](EVAL-AUDIT-2026-09-23.md).

## What changed

- `sonder_runtime/application/evaluation/divergence.py`
  - `DivergencePolicy` says which step fields are decisions (`output`,
    `state`), optionally keeps only named `decision_paths`, and drops
    `ignored_paths` (latency, timestamps, request IDs). The policy is
    schema-versioned and digest-bound.
  - `earliest_divergence` / `replay_divergence` return the first step and
    field whose projected decision content differs, with bounded changed
    paths; a length mismatch diverges at the first missing index.
  - `minimize_failure` reduces a divergent replay. Recorded outputs are only
    a valid oracle for the exact sequence that produced them, so:
    without a baseline the result is the shortest reproducing **prefix**;
    with a `baseline_factory` the oracle is **differential** (fresh baseline
    versus fresh candidate on each subset) and Zeller's ddmin reduces to a
    1-minimal step set. The baseline must first reproduce the full recording.
    Every trial uses fresh evaluators, so stateful sessions restart cleanly.
    The result is replayed twice more and must diverge identically, or a
    `DivergenceError` is raised instead of retaining a flaky record.
    `one_minimal` is true only when differential ddmin converged within its
    evaluation budget.
  - `MinimizedFailure` is immutable and self-verifying: `from_dict`
    re-verifies step, trajectory, and record digests and rejects tampering.
    `reproduce` replays a retained failure against a new candidate.
  - `InMemoryMinimizedFailureStore` is a bounded reference store.
- `sonder_runtime/adapters/evaluation_failure_corpus.py`:
  `JsonMinimizedFailureStore` retains failures durably as
  `<sha256>.json`, written atomically, bounded by count and bytes, with
  digest-named paths only, and re-verified on load.
- `sonder_runtime/application/evaluation/service.py`:
  `earliest_divergence`, `minimize_and_retain_failure`, `retained_failures`,
  and `reproduce_retained_failure` on `EvaluationApplicationService`; the
  store is injected through the `MinimizedFailureStore` port.

## Evidence

`tests/test_eval006_divergence_minimization.py` (11 tests, deterministic
in-process fakes only — no model):

- Noise versus decision: raw comparison diverges at step 0 on
  `latency_ms`; under the policy the earliest divergence is step 7 on `y`.
- A stateful key-value session whose candidate truncates values after a
  mode switch diverges at step 6 of 10. Differential minimization returns
  source steps `(0, 3, 6)` (put, mode, get), marked 1-minimal; an
  independent check confirms that removing any retained step makes a fresh
  baseline and a fresh candidate agree.
- Prefix strategy without a baseline returns the 7-step reproducing prefix
  and never claims 1-minimality.
- An unfaithful baseline, a non-divergent candidate, and a nondeterministic
  candidate are refused.
- Budget exhaustion is reported (`one_minimal` false) while the partial
  result still reproduces.
- Round trip through JSON preserves the digest; altered indexes or outputs
  are rejected.
- The file store retains idempotently, reloads in a new instance, enforces
  its bound, rejects a tampered file, and rejects a non-digest name.
- A retained failure reproduces against the regressed candidate and does not
  reproduce against the fixed one, through the application service.

Mutation check (local): replacing the ddmin call with the unminimized prefix
fails 3 of these tests, so the minimization assertions are load-bearing.

Local verification: the 11 new tests, the 69 pre-existing evaluation tests,
`scripts/check_architecture.py`, `scripts/check_documentation_authority.py`,
`scripts/check_requirement_evidence.py --base-ref origin/main`,
`scripts/check_evidence_documents.py`, `scripts/check_doc_links.py`, and
`git diff --check` all passed.

## Limitations

- No live model session was replayed; all evaluators are deterministic fakes.
  The algorithms do not depend on model behavior, but a live evaluator that is
  not deterministic for a fixed input is refused rather than minimized.
- Differential ddmin needs the caller to supply the baseline behavior; with
  only a recording, minimization is limited to the reproducing prefix.
- Meaningfulness is whatever the caller's `DivergencePolicy` declares; no
  default noise list is inferred.
- The service is not yet composed in the bootstrap graph (see EVAL-001 in the
  audit), so retention is available to callers of the application service,
  not yet from an operator command.
