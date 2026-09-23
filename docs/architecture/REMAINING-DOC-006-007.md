# DOC-006 / DOC-007 — evidence consistency and stale-promise sweep

`scripts/check_evidence_documents.py` performs a bounded sweep of every
`REMAINING-*.md` evidence artifact. It requires an explicit evidence,
verification, or limitation heading and verifies that each referenced focused
test exists. The checker is intentionally independent of the requirement
ledger. Current requirement status is generated from that ledger;
DOC-006 has linked verification evidence, while DOC-007 remains open.

Focused coverage is in `tests/test_evidence_document_consistency.py`. DOC-006
is verified by the same-change evidence gate documented in
`evidence/DOC-006-STATUS-EVIDENCE-2026-09-23.md`; DOC-007 remains
implemented-unverified because semantic stale-promise review is still required.
