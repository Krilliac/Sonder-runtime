# Production artifact readiness

Fan-in validates producer-sealed output through `ArtifactReadinessBarrier`.
Checks cover identity, schema, completion, validation status, SHA-256, byte count,
age and, where applicable, source-task binding and independent verifier receipts.
Consumers never create evidence from whatever bytes they find.

| Boundary | Producer seal | Consumer |
| --- | --- | --- |
| Fleet / master_orchestrate | _run_worker returns ReadyWorkerOutput after completion | run_delegated checks every expected child before provenance aggregation and audit |
| model_fanout | record_result atomically persists readiness_json with a complete answer | MCP and HTTP synthesis share the validated source builder |
| parallel_generate_run | Candidate wrapper seals after the existing code verifier succeeds | Validation before pass counting and winner selection |
| parallel_generate_run_languages | Same wrapper, including language and code in the digest | Validation before language winner selection |

Fleet and master share one synthesis boundary; `run_inline` has no worker
fan-in. Master already sealed outputs on this base revision. The change reports
readiness-rejected slots to synthesis instead of aborting the whole join.
Children that failed or aborted without output stay omitted from the audit
prompt exactly as before, and the repository `HOST AGGREGATION SCOPE`
`children=` line lists only accepted children. There is no extra Fleet
readiness table or database read.

Valid slots retain the previous synthesis bytes exactly. Rejected slots
contribute only identity/status, never output. Model fanout emits a `not_ready`
source; master emits an `ARTIFACT REJECTED` marker; parallel generation emits
`NOT READY` for a candidate that claimed success without valid evidence and
cannot select that code. Candidates that already failed are not artifacts: they
are never sealed, never selected, and keep their pre-fan-in diagnostics
byte-for-byte. When there is no usable output, the
no-result/refusal behavior remains explicit. Active model fanout runs still
cannot be synthesized.

Public progress receipts keep their diagnostic preview contract. The internal
SQLite migration adds one default-empty field without backfilling legacy rows.
Historical unsealed answers remain readable as diagnostics but are not accepted
as complete synthesis artifacts.

Configured code/objective verifiers require a matching receipt captured
separately by the host. Master keeps the base revision's check: fan-in
recomputes each child's objective-coverage receipt from the rendered output
and never trusts the producer's copy. Model fanout and parallel generation do
not rerun verifiers at fan-in.
Master retains its age bound: `min(24h, max(15m, fanout elapsed + 5m))`.
Persisted model-fanout answers are digest-bound and stay valid for synthesis
for as long as fanout retention keeps their run. Parallel generation uses the
24-hour default. Model-fanout synthesis still requires at least two answered
results, and now at least two validated-complete artifacts; master refuses
synthesis when no child artifact validates.

Provider token-limit termination and storage truncation are rejected.
The shared Python candidate generator uses an optional thread-local completion
metadata observer to prevent sibling responses overwriting each other's
completion evidence. Ordinary callers retain their return values and public
metadata behavior. No probe, model request, retry, wait or background thread
is added.

See [verification](readiness-scoped-review-validation.md) for measurements and
[scoped reviewer context](SCOPED-REVIEWER-CONTEXT.md) for role changes.
