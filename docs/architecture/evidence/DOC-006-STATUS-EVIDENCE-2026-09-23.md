# DOC-006: status evidence is checked with its evidence

DOC-006 is checked in the same change that adds this evidence document, the
revision 3 ledger record, the generated status projections, and the canary
expectation for the checked state.

Evidence:

- `scripts/check_requirement_evidence.py` validates that a newly checked
  requirement has a newly added `verified` ledger revision with evidence paths,
  and that the generated JSON and Markdown projections match the source.
- `scripts/check_evidence_documents.py` validates evidence-document links and
  limitation disclosures.
- `tests/test_document_authority.py` asserts that DOC-006 is checked while the
  other DOC-001 through DOC-007 items that remain unverified stay open.
- `tests/test_evidence_document_consistency.py` runs the repository-wide
  evidence-document consistency check.
- The pull request's exact-head CI run
  [`35837919825`](https://github.com/Krilliac/Sonder-runtime/actions/runs/35837919825)
  completed the `Validate master-spec evidence ledger` and
  `Validate evidence changes against pull request base` steps successfully for
  head `cffd62d9f27eb48f7e2571de962c91865dd3faf6`. The full CI job later
  completed successfully at that exact source SHA. The ledger's `verified_sha`
  identifies this tested gate implementation; this evidence document and the
  DOC-006 checkbox are added in the subsequent change.

Local verification from the same worktree:

- `tests/test_evidence_document_consistency.py` and
  `tests/test_document_authority.py`: 6 passed.
- `scripts/check_requirement_evidence.py`: passed.
- `scripts/check_evidence_documents.py`: passed.
- `git diff --check`: passed.

Limits: this gate checks that status claims are coupled to repository evidence
and generated projections. It does not judge the semantic quality of every
claim, prove resistance to direct or administrative bypass, or prove
append-only history against a privileged repository administrator.
