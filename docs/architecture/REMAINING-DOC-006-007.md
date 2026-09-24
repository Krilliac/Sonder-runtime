# DOC-006 / DOC-007 — evidence consistency and stale-promise sweep

`scripts/check_evidence_documents.py` performs a bounded sweep of every
`REMAINING-*.md` evidence artifact. It requires an explicit evidence,
verification, or limitation heading and verifies that each referenced focused
test exists. The checker is intentionally independent of the requirement
ledger. Current requirement status is generated from that ledger;
DOC-006 and DOC-007 have linked verification evidence.

Focused coverage is in `tests/test_evidence_document_consistency.py`. DOC-006
is verified by the same-change evidence gate documented in
`evidence/DOC-006-STATUS-EVIDENCE-2026-09-23.md`. DOC-007 is verified by the
product-document status-vocabulary gate in
`scripts/check_documentation_authority.py`, covered by
`tests/test_document_authority.py` and documented in
`evidence/DOC-007-STATUS-VOCABULARY-2026-09-23.md`; semantic review of each
status row remains ordinary review work.
