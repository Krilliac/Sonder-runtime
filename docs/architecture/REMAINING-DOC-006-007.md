# DOC-006 / DOC-007 — evidence consistency and stale-promise sweep

`scripts/check_evidence_documents.py` performs a bounded sweep of every
`REMAINING-*.md` evidence artifact. It requires an explicit evidence,
verification, or limitation heading and verifies that each referenced focused
test exists. The checker is intentionally independent of the requirement
ledger: ledger entries remain `planned` until end-to-end verification exists.

Focused coverage is in `tests/test_evidence_document_consistency.py`.
