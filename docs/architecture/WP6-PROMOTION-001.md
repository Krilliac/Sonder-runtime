# WP6-PROMOTION-001 — measured promotion gates

Status: implemented slice; formal checklist remains unchanged.

`domain/promotion/measured.py` defines immutable, bounded evidence for the
five promotion areas: skills, routing, memory, models, and self-modification.
`application/promotion/gates.py` evaluates explicit per-area policies.

An approval requires all configured metric minimums, regression limits,
holdout success, required provenance identifiers, and a rollback reference.
The evaluator is side-effect free: it authorizes a candidate and emits a
stable evidence digest, but does not activate it or perform rollback. Rejected
decisions include named failed gates for operator/audit feedback.

Focused coverage verifies successful approval, every safety gate, missing
metrics, unknown policy, digest sensitivity, and invalid/non-finite evidence.
This slice does not claim the full promotion pipeline, durable decision log,
or formal requirement completion.
