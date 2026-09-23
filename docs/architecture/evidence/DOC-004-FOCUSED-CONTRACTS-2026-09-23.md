# DOC-004 focused-contract verification

DOC-004 requires `ARCHITECTURE.md`, `SECURITY.md`, `SELFMOD.md`,
`TRAINING.md`, `CLIENT.md`, and `MOBILE_HOST_CONTROL.md` to stay focused on
current behavior and to link their unfinished implementation work to the
master specification. [PR #549](https://github.com/Krilliac/Sonder-runtime/pull/549)
implements that contract at commit `27a476f7eb2a67eaa6fa05de23035e183ffeeeff`
on baseline `494f2397601b784abe82d5131693f16baad709d4`.

## What changed

- `ARCHITECTURE.md` already linked the master specification; the other five
  contracts now carry a contract-scope note that links it within their first
  twelve lines.
- Each contract ends with one `## Behavior status` table. Unfinished work
  appears only as a `Proposed` row citing open master-spec requirements:
  ARCH-002, ARCH-003, and CORE-005 (architecture); SEC-001 (security);
  SELFMOD-003 and EVAL-007 (self-modification); TRAIN-007 (training); API-002
  (client); and API-007 (mobile host control).
- Two future-tense sentences were restated as current behavior after checking
  the code: `script_run` enforcing `deny-*` modes fail closed with
  `exact_execution_handoff_unavailable` (`server.py`), and the training planner
  rejects `--allow-cpu-offload` (`adaptive_training.py`).
- `scripts/check_documentation_authority.py` enforces the early master-spec
  link, exactly one status table, known labels and requirement IDs, and that
  every `Proposed` row cites a requirement that is still unchecked and not
  verified. When a cited requirement is completed, the gate fails until the
  row is restated as current behavior.

## Verification

At `27a476f7`, on Windows with Python 3.12.10:
`scripts/check_documentation_authority.py`, `scripts/check_doc_links.py`,
`scripts/check_evidence_documents.py`, `scripts/check_architecture.py`, and
`scripts/check_requirement_evidence.py --base-ref origin/main` all exited 0.
`tests/test_document_authority.py`, `tests/test_remaining_doc_001_005.py`,
`tests/test_evidence_document_consistency.py`, and
`tests/test_check_doc_links.py` passed 28 tests. Of these,
`tests/test_document_authority.py` adds six fixture-based negative cases. They
cover a late spec link, unknown labels and IDs, stale `Proposed` rows,
forward-looking prose, slice logs, and duplicate sections.

The gate is load-bearing. When the `27a476f7` checker was run against the
seven baseline product documents, it reported 168 problems. These included
five focused contracts with no early master-spec link, seven missing status
tables, and two unlabeled future-tense phrases.

Hosted CI for the exact implementation head:
[CI run 35932793054](https://github.com/Krilliac/Sonder-runtime/actions/runs/35932793054)
succeeded on Ubuntu at `27a476f7`, including requirement evidence, doc links,
generated-reference freshness, documentation authority, and the full test
suite (16471 passed, 104 skipped). The paired app-build run 35932792997 was still queued when this record
was written, and it is not claimed here.

## Limitations

The gate checks structure, linkage, vocabulary, and requirement state. It does
not prove that every sentence or status row is factually current. Row claims
were checked against the code by hand for the cited behaviors only.
