# MODEL-007 and TRAIN-002/003/004/008

Status: isolated application contracts with focused verification.

`ControlledEscalation` requires every candidate route to be declared by the
request, orders only those routes, and permits escalation only when uncertainty
or verifier failure is present and the bounded escalation budget remains. The
decision records the trigger, count, and whether a later outcome helped.

`QualifiedDependencyLock` is separate from runtime dependencies and compares
the complete sorted dependency tuple, including exact versions, sources, and
artifact digests, plus an exact execution-environment digest.

`DatasetQualification` requires source, license, privacy-review, dedup, and
independent train/evaluation snapshot identities. Admission reports explicit
failures for unapproved provenance, privacy, license, duplicate/contaminated,
empty, or non-separated data.

`TrainingEvaluationPolicy` gates behavior, regression, latency, memory, context,
and tool-use dimensions. `CheapLearningFirst` chooses the first reliable method
in the non-weight-learning order (memory, retrieval, skill, routing, few-shot)
before weight training.

Verification: `tests/test_remaining_model_training.py` covers bounded and
request-scoped escalation, exact lock matching, dataset admission, all six
training evaluation dimensions, and cheap-learning-first selection. Formal
master-spec checkboxes are intentionally unchanged.
