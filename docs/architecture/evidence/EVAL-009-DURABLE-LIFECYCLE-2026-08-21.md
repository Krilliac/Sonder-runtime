# EVAL-009 durable evaluation lifecycle evidence — 2026-08-21

This bounded slice connects the existing fail-closed `ProposalLifecycle` state
machine to a durable append-only repository. `EvaluationLifecycleService`
delegates validation, attended promotion, and attended rollback to the existing
domain lifecycle, then records only successful mutations. The exact immutable
promotion evidence payload and digest are retained in the lifecycle event.

The adapter namespaces events as `evaluation:{proposal_id}` over the canonical
hash-chained session repository. Reopening the SQLite repository preserves the
promotion/rollback history and integrity verification. Failed unattended
promotion or rollback does not append an event, so rejected decisions cannot
look like completed lifecycle transitions.

Evidence:

- `tests/test_eval009_durable_lifecycle.py`
- `python -m pytest -q tests/test_eval009_durable_lifecycle.py`
- `python scripts/check_architecture.py`
- `python scripts/check_evidence_documents.py`

This is slice evidence only; the formal master checklist and requirement ledger
remain unchanged.
