# Issue #510: strategy evaluation and production gate boundary

The held-out strategy cases call the real `StrategyController` through
`StrategyTraceService.record()`, twice in distinct empty sealed checkpoint
repositories. The evaluator reads back the decisions and attempt history,
checks the parent graph, compares exact expected actions, and binds the graph
and host-held case digests to an offline `EvaluationResult`. Canaries cover identical and
cosmetically changed repeats, real measured progress, regression, alternate
tool routing, critic failure/success selection, model rotation, descendant and
parallel choices, exhausted budget, unknown effects after a crash, and an
irrelevant-memory negative control. The separate codegen observer canary checks
that host-recorded attempts and call counts survive through the same trace.

The shared EVAL suite binds policy version, model-role mapping digest, visible
tool manifest digest, selected memory manifest digest, skill catalog digest,
and runtime/environment/hardware digest as suite dimensions. Each result also
binds the authenticated attempt graph digest and a digest of the declared
context in its provenance. A new `strategy` promotion kind uses the existing
sample-size, Wilson confidence, baseline regression, replay, shadow and canary
gates. Its gate rejects generic/unbound results, repeated attempt graphs, and
repeat held-out cases even if reruns produce different attempt graphs.
Every synthetic result has the typed `synthetic_policy_canary` evidence class.
Expected-action agreement is a policy regression reading, never an independent
task outcome: the `independent_task_receipts` promotion gate remains false even
when many sealed canaries replay, satisfy confidence thresholds, and report
healthy shadow and canary observations. The production host admits only that
known evidence class until it has an independent verifier for real task
receipts. Synthetic case outcomes cannot populate measured task-success fields.
The ablation comparison returns only per-metric differences for paired
synthetic cases; it is not a causal estimate of a live task benefit.

Resource fields (attempts, model/tool/verifier calls, tokens, wall time,
descendants, switches, replans, critic and top-tier calls) reflect only the
quantities the host recorded on those attempts. The fixed synthetic cases
set their own quantities; no measured task-completion lift, context peak,
concurrency peak, real cost saving, or hardware benchmark is claimed.

The actual `Application` graph now exposes a cached evaluation service backed
by the session event repository, durable minimized-failure directory, and the
shared per-kind promotion gate. Evaluation is unavailable until the host
supplies bounded repository, tool, and memory corpus readers; a missing source
causes `begin_evaluation` and gated evidence to fail closed. The bootstrap
does not fabricate records to make the corpus appear covered. Although events
and failures persist, the in-memory proposal state is not reconstructed on
restart; post-restart proposal approval is not supported by this slice.
The host also pins the complete corpus digest at evaluation start and rejects
results, subsequent phases, approval, and promotion if any source changes.

Digest comparisons detect accidental or unauthorized changes relative to
host-held expected values, but caller-provided runtime manifests are not a
signature or independent assertion of the environment. A candidate that can
write its grader or supply its own expected actions cannot turn these canaries
into trustworthy held-out proof. Real production conformance, isolated
grader authority, trusted corpus source bindings, native host evidence, and
shadow/canary outcome measurements remain required before promotion is
qualified. Strategy and selfmod results must not authorize unattended rollout.
