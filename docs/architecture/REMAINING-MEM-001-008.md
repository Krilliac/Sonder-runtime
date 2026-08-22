# REMAINING-MEM-001–008 — memory policy integration

Status: isolated implementation slice; formal master-spec checkboxes remain
unchanged.

`sonder_runtime.application.memory.memory_policy` closes the application-layer
gap between the existing SQLite memory ports and WP6's typed memory records.
It does not add a second store or bypass `MemoryRepositoryAdapter`.

The policy matrix explicitly covers working, episodic, semantic, procedural,
preference, project, failure, and artifact memory.  Each class declares
admission confidence/provenance/evidence requirements, retrieval scope and
freshness behavior, privacy defaults, promotion eligibility, export, and
deletion policy.  `evaluate_write` and `evaluate_retrieval` are pure decisions;
secret material is rejected at this memory boundary and retrieval decisions
carry score components, provenance, freshness, confidence, and exclusion
reasons.

`TemporalTruth` represents valid-from/until intervals, superseding and
contradiction links, source trust, confidence, exponential decay, and explicit
revalidation.  Relationships are validated so an assertion cannot supersede
and contradict the same record or relate to itself.

`EmbeddingIdentity` binds model, revision, dimensions, normalization,
truncation, and serving implementation.  `EmbeddingBinding` validates vectors
against that identity and records a deterministic vector digest, complementing
the existing store's model/revision/dimension columns rather than silently
assuming that metadata.

`link_procedural_promotion` accepts only evidence-backed, non-contradictory WP6
procedural memories and emits linkage containing source interactions, baseline
and candidate digests, and a rollback reference.  This is the promotion
handoff contract; activation and rollback remain owned by the existing WP7
deployment/rollback service.

Focused verification: `tests/test_remaining_memory_policy.py`.

