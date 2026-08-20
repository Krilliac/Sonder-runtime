# TRAIN-002/003/004/008 — Training qualification and cheap-learning gates

Status: implemented as bounded application contracts with focused tests.

`QualifiedDependencyLock.verify_exact` compares the complete training-only
dependency record (name, exact version, source, and artifact digest), rejects
missing, extra, duplicate, or mismatched records, and binds the result to the
exact execution-environment digest. It is intentionally separate from runtime
dependency resolution.

`DatasetQualification.validate` requires approved privacy review, source and
source revision, allowed license, dedup method and snapshot-bound dedup
evidence, non-empty data, zero duplicate/contamination/overlap counts, and
distinct train/evaluation snapshots.

`TrainingEvaluationPolicy` is a training-specific fail-closed gate for
behavior, regression delta, latency, memory, context, and tool use. Metrics
must be finite; resource and regression values cannot be negative.

`CheapLearningFirst.execute` consumes explicit method ports in the order
memory, retrieval, skill, routing, and few-shot. It invokes the first reliable
port and never invokes weight training after success. Weight training is the
final fallback through its explicit training port, preserving the attended
training boundary.

Verification: `tests/test_remaining_model_training.py` and
`tests/test_remaining_training_002_003_004_008.py` cover exact lock mismatch
reasons, dataset evidence failures, all training gate dimensions, and both
cheap-learning orchestration paths. The master checklist and audit remain
unchanged.
