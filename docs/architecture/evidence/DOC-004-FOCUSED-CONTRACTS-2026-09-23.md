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

## Review correction and re-verification

Review of the first verified revision found that `CLIENT.md` described
`sonder_client.py` as a standalone, stdlib-only, single-file download.
Since `309f46f2`, the file imports six `sonder_runtime` client adapters, so a
lone copy fails with `ModuleNotFoundError`. The gate at `27a476f7` checked
structure and vocabulary, so it could not detect that false Implemented row.

Commit `cb1a9a22` made these corrections:

- `CLIENT.md` now documents the checkout requirement and has an Unsupported
  row for single-file copies. `sonder_client.py` examples use https.
- `test_thin_client_documentation_matches_its_checkout_dependency` proves the
  dependency both ways: a lone copy fails, and a checkout imports.
- The underived "46 direct MCP call paths" count was removed.
- The gate now has wider stale-promise and slice-log patterns.
- Implemented rows must cite IDs whose latest ledger status is at least
  implemented_unverified.
- A planted-document test goes through `check()`. It was mutation-verified:
  unwiring the product-document gate makes it fail.
- A four-column-row test covers the column check.
- `scripts/check_doc_links.py` now scans the 151 evidence records under
  `docs/architecture/evidence/`. Before this change, "doc links passed" claims
  about evidence records were vacuous.

The earlier verified revision is superseded by one bound to `cb1a9a22`:
[CI run 35938479949](https://github.com/Krilliac/Sonder-runtime/actions/runs/35938479949)
succeeded on Ubuntu at `cb1a9a22`. That covers requirement evidence, doc links
(now including evidence records), generated-reference freshness,
documentation authority, and the full test suite (16481 passed, 104 skipped).
The paired app-build runs were still queued, so they are not claimed.

## Limitations

The gate checks structure, linkage, vocabulary, and requirement state. It does
not prove that every sentence or status row is factually current. Row claims
were checked against the code by hand for the cited behaviors only; the
`CLIENT.md` correction above shows why that review remains necessary.
